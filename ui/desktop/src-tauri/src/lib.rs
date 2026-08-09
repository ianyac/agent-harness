pub mod commands;
pub mod lifecycle;
pub mod readiness;
pub mod state;

use std::{
    fs,
    future::Future,
    io,
    path::{Path, PathBuf},
    sync::atomic::{AtomicBool, Ordering},
    time::{Duration, Instant},
};

use tauri::Manager as _;

const BOOTSTRAP_DIRECTORY_NAME: &str = "service-bootstrap";
const EXIT_SHUTDOWN_TIMEOUT: Duration = Duration::from_secs(20);
const EXIT_SHUTDOWN_RETRY_INTERVAL: Duration = Duration::from_millis(25);

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum ExitDecision {
    ShutdownThenExit,
    AllowExit,
}

#[derive(Default)]
struct ExitCoordinator {
    exit_requested: AtomicBool,
}

impl ExitCoordinator {
    fn on_exit_requested(&self) -> ExitDecision {
        if self.exit_requested.swap(true, Ordering::AcqRel) {
            ExitDecision::AllowExit
        } else {
            ExitDecision::ShutdownThenExit
        }
    }
}

fn canonical_bootstrap_directory(app_data_dir: &Path) -> io::Result<PathBuf> {
    fs::create_dir_all(app_data_dir)?;
    let canonical_app_data = app_data_dir.canonicalize()?;
    let directory = canonical_app_data.join(BOOTSTRAP_DIRECTORY_NAME);
    match fs::symlink_metadata(&directory) {
        Ok(metadata) if metadata.file_type().is_symlink() || !metadata.is_dir() => {
            return Err(io::Error::other("bootstrap directory is unsafe"));
        }
        Ok(_) => {}
        Err(error) if error.kind() == io::ErrorKind::NotFound => fs::create_dir(&directory)?,
        Err(error) => return Err(error),
    }
    let canonical = directory.canonicalize()?;
    if canonical.parent() != Some(canonical_app_data.as_path()) {
        return Err(io::Error::other("bootstrap directory is unsafe"));
    }
    Ok(canonical)
}

fn should_retry_exit_shutdown(error: lifecycle::LifecycleError, before_deadline: bool) -> bool {
    before_deadline
        && matches!(
            error,
            lifecycle::LifecycleError::Busy | lifecycle::LifecycleError::ProcessControlFailed
        )
}

async fn shutdown_before_exit(lifecycle: &lifecycle::ServiceLifecycle) {
    let deadline = Instant::now() + EXIT_SHUTDOWN_TIMEOUT;
    loop {
        match lifecycle.shutdown().await {
            Ok(()) => return,
            Err(error) if should_retry_exit_shutdown(error, Instant::now() < deadline) => {
                tokio::time::sleep(EXIT_SHUTDOWN_RETRY_INTERVAL).await;
            }
            Err(_) => return,
        }
    }
}

async fn complete_graceful_exit(shutdown: impl Future<Output = ()>, reissue_exit: impl FnOnce()) {
    shutdown.await;
    reissue_exit();
}

pub fn run() {
    let app = tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_notification::init())
        .plugin(tauri_plugin_opener::init())
        .invoke_handler(commands::handler())
        .setup(|app| {
            let bootstrap_workspace = canonical_bootstrap_directory(&app.path().app_data_dir()?)?;
            let diagnostic_log_path = app.path().app_log_dir()?.join("sidecar.log");
            let lifecycle =
                lifecycle::ServiceLifecycle::for_tauri(app.handle().clone(), diagnostic_log_path);
            app.manage(lifecycle.clone());
            app.manage(ExitCoordinator::default());
            tauri::async_runtime::spawn(async move {
                let _ = lifecycle.start(bootstrap_workspace).await;
            });
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while running Harness");
    app.run(|handle, event| {
        if let tauri::RunEvent::ExitRequested { api, .. } = event {
            let decision = handle.state::<ExitCoordinator>().on_exit_requested();
            if decision == ExitDecision::ShutdownThenExit {
                api.prevent_exit();
                let handle = handle.clone();
                let lifecycle = handle
                    .state::<lifecycle::ServiceLifecycle>()
                    .inner()
                    .clone();
                tauri::async_runtime::spawn(async move {
                    complete_graceful_exit(shutdown_before_exit(&lifecycle), || handle.exit(0))
                        .await;
                });
            }
        }
    });
}

#[cfg(test)]
mod tests {
    use std::{fs, sync::Mutex};

    use super::*;

    #[test]
    fn tauri_config_targets_stable_harness_executable() {
        let config: serde_json::Value =
            serde_json::from_str(include_str!("../tauri.conf.json")).unwrap();

        assert_eq!(
            config
                .get("mainBinaryName")
                .and_then(|value| value.as_str()),
            Some("Harness")
        );
    }

    #[test]
    fn application_bootstrap_directory_is_created_below_app_data_and_canonicalized() {
        let root = std::env::temp_dir().join(format!(
            "agent-harness-bootstrap-{}-{}",
            std::process::id(),
            rand::random::<u64>()
        ));

        let bootstrap = canonical_bootstrap_directory(&root)
            .expect("application bootstrap directory must be created");

        assert!(bootstrap.is_dir());
        let canonical_root = root
            .canonicalize()
            .expect("app data root must canonicalize");
        assert_eq!(bootstrap.parent(), Some(canonical_root.as_path()));
        assert_eq!(bootstrap, bootstrap.canonicalize().unwrap());
        fs::remove_dir_all(root).expect("temporary app data must be removed");
    }

    #[cfg(unix)]
    #[test]
    fn application_bootstrap_directory_rejects_a_symlink_escape() {
        let root = std::env::temp_dir().join(format!(
            "agent-harness-bootstrap-root-{}-{}",
            std::process::id(),
            rand::random::<u64>()
        ));
        let outside = std::env::temp_dir().join(format!(
            "agent-harness-bootstrap-outside-{}-{}",
            std::process::id(),
            rand::random::<u64>()
        ));
        fs::create_dir_all(&root).expect("temporary app data must be created");
        fs::create_dir_all(&outside).expect("outside directory must be created");
        std::os::unix::fs::symlink(&outside, root.join(BOOTSTRAP_DIRECTORY_NAME))
            .expect("escaping bootstrap symlink must be created");

        assert!(canonical_bootstrap_directory(&root).is_err());

        fs::remove_dir_all(root).expect("temporary app data must be removed");
        fs::remove_dir_all(outside).expect("outside directory must be removed");
    }

    #[test]
    fn graceful_exit_prevents_only_the_first_request_and_reissues_once() {
        let coordinator = ExitCoordinator::default();

        assert_eq!(
            coordinator.on_exit_requested(),
            ExitDecision::ShutdownThenExit
        );
        assert_eq!(coordinator.on_exit_requested(), ExitDecision::AllowExit);
        assert_eq!(coordinator.on_exit_requested(), ExitDecision::AllowExit);
    }

    #[test]
    fn graceful_exit_awaits_shutdown_before_reissuing_exactly_once() {
        tauri::async_runtime::block_on(async {
            let steps = Mutex::new(Vec::new());

            complete_graceful_exit(
                async {
                    steps
                        .lock()
                        .expect("steps lock must be available")
                        .push("shutdown");
                },
                || {
                    steps
                        .lock()
                        .expect("steps lock must be available")
                        .push("exit")
                },
            )
            .await;

            assert_eq!(
                *steps.lock().expect("steps lock must be available"),
                vec!["shutdown", "exit"]
            );
        });
    }

    #[test]
    fn exit_cleanup_retries_busy_and_process_control_failure_only_while_bounded() {
        assert!(should_retry_exit_shutdown(
            lifecycle::LifecycleError::Busy,
            true
        ));
        assert!(should_retry_exit_shutdown(
            lifecycle::LifecycleError::ProcessControlFailed,
            true
        ));
        assert!(!should_retry_exit_shutdown(
            lifecycle::LifecycleError::ProcessControlFailed,
            false
        ));
        assert!(!should_retry_exit_shutdown(
            lifecycle::LifecycleError::RestartRefused,
            true
        ));
    }
}
