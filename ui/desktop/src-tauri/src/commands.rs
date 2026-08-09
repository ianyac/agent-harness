use std::{fs, path::PathBuf};

use tauri::{AppHandle, Manager as _, Runtime, State};
use tauri_plugin_dialog::{DialogExt as _, FilePath};
use tauri_plugin_notification::{NotificationExt as _, PermissionState};
use tauri_plugin_opener::OpenerExt as _;

use crate::{
    lifecycle::{LifecycleError, ServiceLifecycle},
    state::ServiceConnection,
};

pub const TITLE_CHARACTER_LIMIT: usize = 128;
pub const BODY_CHARACTER_LIMIT: usize = 512;

const SERVICE_UNAVAILABLE: &str = "Local service is unavailable.";
const WORKSPACE_SELECTION_FAILED: &str = "Workspace selection failed.";
const NOTIFICATION_UNAVAILABLE: &str = "Notification is unavailable.";
const LOGS_UNAVAILABLE: &str = "Logs are unavailable.";
const RESTART_UNAVAILABLE: &str = "Local service restart is unavailable.";

type CommandResult<T> = Result<T, String>;

#[derive(Clone, Copy)]
enum NativeNotificationPermission {
    Granted,
    Denied,
    Prompt,
}

trait NotificationAuthority {
    fn permission(&self) -> Result<NativeNotificationPermission, ()>;
    fn request_permission(&self) -> Result<NativeNotificationPermission, ()>;
    fn send(&self, title: &str, body: &str) -> Result<(), ()>;
}

impl<R: Runtime> NotificationAuthority for AppHandle<R> {
    fn permission(&self) -> Result<NativeNotificationPermission, ()> {
        self.notification()
            .permission_state()
            .map(map_notification_permission)
            .map_err(|_| ())
    }

    fn request_permission(&self) -> Result<NativeNotificationPermission, ()> {
        self.notification()
            .request_permission()
            .map(map_notification_permission)
            .map_err(|_| ())
    }

    fn send(&self, title: &str, body: &str) -> Result<(), ()> {
        self.notification()
            .builder()
            .title(title)
            .body(body)
            .show()
            .map_err(|_| ())
    }
}

fn map_notification_permission(permission: PermissionState) -> NativeNotificationPermission {
    match permission {
        PermissionState::Granted => NativeNotificationPermission::Granted,
        PermissionState::Denied => NativeNotificationPermission::Denied,
        PermissionState::Prompt | PermissionState::PromptWithRationale => {
            NativeNotificationPermission::Prompt
        }
    }
}

fn fixed_error(message: &'static str) -> String {
    message.to_owned()
}

fn connection_result(connection: Option<ServiceConnection>) -> CommandResult<ServiceConnection> {
    connection.ok_or_else(|| fixed_error(SERVICE_UNAVAILABLE))
}

fn workspace_path_to_utf8(path: PathBuf) -> CommandResult<String> {
    path.into_os_string()
        .into_string()
        .map_err(|_| fixed_error(WORKSPACE_SELECTION_FAILED))
}

fn canonical_workspace_selection(selection: Option<PathBuf>) -> CommandResult<Option<String>> {
    let Some(selection) = selection else {
        return Ok(None);
    };
    if !selection.is_absolute() || !selection.is_dir() {
        return Err(fixed_error(WORKSPACE_SELECTION_FAILED));
    }
    let canonical = selection
        .canonicalize()
        .map_err(|_| fixed_error(WORKSPACE_SELECTION_FAILED))?;
    if canonical != selection || !canonical.is_dir() {
        return Err(fixed_error(WORKSPACE_SELECTION_FAILED));
    }
    workspace_path_to_utf8(canonical).map(Some)
}

fn normalize_dialog_selection(selection: Option<FilePath>) -> CommandResult<Option<String>> {
    match selection {
        None => Ok(None),
        Some(FilePath::Path(path)) => canonical_workspace_selection(Some(path)),
        Some(FilePath::Url(_)) => Err(fixed_error(WORKSPACE_SELECTION_FAILED)),
    }
}

fn send_notification(
    authority: &impl NotificationAuthority,
    title: &str,
    body: &str,
) -> CommandResult<()> {
    if title.chars().count() > TITLE_CHARACTER_LIMIT
        || body.chars().count() > BODY_CHARACTER_LIMIT
        || title.contains('\0')
        || body.contains('\0')
    {
        return Err(fixed_error(NOTIFICATION_UNAVAILABLE));
    }
    let permission = authority
        .permission()
        .map_err(|_| fixed_error(NOTIFICATION_UNAVAILABLE))?;
    let permission = match permission {
        NativeNotificationPermission::Prompt => authority
            .request_permission()
            .map_err(|_| fixed_error(NOTIFICATION_UNAVAILABLE))?,
        settled => settled,
    };
    if !matches!(permission, NativeNotificationPermission::Granted) {
        return Err(fixed_error(NOTIFICATION_UNAVAILABLE));
    }
    authority
        .send(title, body)
        .map_err(|_| fixed_error(NOTIFICATION_UNAVAILABLE))
}

fn prepare_log_directory(path: &std::path::Path) -> CommandResult<PathBuf> {
    fs::create_dir_all(path).map_err(|_| fixed_error(LOGS_UNAVAILABLE))?;
    path.canonicalize()
        .map_err(|_| fixed_error(LOGS_UNAVAILABLE))
}

fn restart_result(
    result: Result<ServiceConnection, LifecycleError>,
) -> CommandResult<ServiceConnection> {
    result.map_err(|_| fixed_error(RESTART_UNAVAILABLE))
}

#[tauri::command]
pub async fn service_connection(
    lifecycle: State<'_, ServiceLifecycle>,
) -> CommandResult<ServiceConnection> {
    connection_result(lifecycle.connection().await)
}

#[tauri::command]
pub async fn choose_workspace(app: AppHandle) -> CommandResult<Option<String>> {
    let (sender, receiver) = tokio::sync::oneshot::channel();
    app.dialog().file().pick_folder(move |selection| {
        let _ = sender.send(selection);
    });
    let selection = receiver
        .await
        .map_err(|_| fixed_error(WORKSPACE_SELECTION_FAILED))?;
    normalize_dialog_selection(selection)
}

#[tauri::command]
pub fn notify(app: AppHandle, title: String, body: String) -> CommandResult<()> {
    send_notification(&app, &title, &body)
}

#[tauri::command]
pub fn open_logs(app: AppHandle) -> CommandResult<()> {
    let configured = app
        .path()
        .app_log_dir()
        .map_err(|_| fixed_error(LOGS_UNAVAILABLE))?;
    let directory = prepare_log_directory(&configured)?;
    let directory = directory
        .into_os_string()
        .into_string()
        .map_err(|_| fixed_error(LOGS_UNAVAILABLE))?;
    app.opener()
        .open_path(directory, None::<String>)
        .map_err(|_| fixed_error(LOGS_UNAVAILABLE))
}

#[tauri::command]
pub async fn restart_service(
    lifecycle: State<'_, ServiceLifecycle>,
) -> CommandResult<ServiceConnection> {
    restart_result(lifecycle.restart_once().await)
}

#[tauri::command]
pub fn quit_app(app: AppHandle) {
    app.exit(0);
}

macro_rules! command_registry {
    ($($command:ident),+ $(,)?) => {
        pub const COMMAND_NAMES: &[&str] = &[$(stringify!($command)),+];

        pub fn handler() -> impl Fn(tauri::ipc::Invoke<tauri::Wry>) -> bool + Send + Sync + 'static {
            tauri::generate_handler![$($command),+]
        }
    };
}

command_registry!(
    service_connection,
    choose_workspace,
    notify,
    open_logs,
    restart_service,
    quit_app,
);

#[cfg(test)]
mod tests {
    use std::{fs, path::PathBuf};

    use super::*;
    use crate::{lifecycle::LifecycleError, state::ServiceConnection};

    fn unique_temp_path(label: &str) -> PathBuf {
        std::env::temp_dir().join(format!(
            "agent-harness-{label}-{}-{}",
            std::process::id(),
            rand::random::<u64>()
        ))
    }

    #[test]
    fn exact_command_registry_exposes_only_the_six_narrow_operations() {
        assert_eq!(
            COMMAND_NAMES,
            [
                "service_connection",
                "choose_workspace",
                "notify",
                "open_logs",
                "restart_service",
                "quit_app",
            ]
        );
    }

    #[test]
    fn service_connection_clones_memory_state_and_uses_a_fixed_unavailable_error() {
        let token = "s".repeat(43);
        let connection = ServiceConnection::new("http://127.0.0.1:49152".to_owned(), token.clone());

        let cloned = connection_result(Some(connection)).expect("connection must be available");
        assert_eq!(cloned.base_url(), "http://127.0.0.1:49152");
        assert_eq!(cloned.token(), token);
        assert_eq!(
            connection_result(None)
                .err()
                .expect("missing connection must fail"),
            SERVICE_UNAVAILABLE
        );
        assert!(!SERVICE_UNAVAILABLE.contains(&token));
    }

    #[test]
    fn workspace_selection_returns_none_on_cancel_and_a_canonical_directory() {
        let directory = unique_temp_path("workspace");
        fs::create_dir_all(&directory).expect("temporary workspace must be created");
        let canonical = directory
            .canonicalize()
            .expect("workspace must canonicalize");

        assert_eq!(canonical_workspace_selection(None).unwrap(), None);
        assert_eq!(
            canonical_workspace_selection(Some(canonical.clone())).unwrap(),
            Some(canonical.to_string_lossy().into_owned())
        );

        fs::remove_dir_all(canonical).expect("temporary workspace must be removed");
    }

    #[test]
    fn workspace_selection_rejects_files_and_symlink_aliases_without_echoing_paths() {
        let root = unique_temp_path("workspace-invalid");
        let directory = root.join("real");
        let file = root.join("file.txt");
        fs::create_dir_all(&directory).expect("temporary workspace must be created");
        fs::write(&file, b"not a directory").expect("temporary file must be created");

        let file_error = canonical_workspace_selection(Some(file.clone())).unwrap_err();
        assert_eq!(file_error, WORKSPACE_SELECTION_FAILED);
        assert!(!file_error.contains(file.to_string_lossy().as_ref()));

        #[cfg(unix)]
        {
            let alias = root.join("alias");
            std::os::unix::fs::symlink(&directory, &alias).expect("symlink must be created");
            let alias_error = canonical_workspace_selection(Some(alias.clone())).unwrap_err();
            assert_eq!(alias_error, WORKSPACE_SELECTION_FAILED);
            assert!(!alias_error.contains(alias.to_string_lossy().as_ref()));
        }

        fs::remove_dir_all(root).expect("temporary workspace root must be removed");
    }

    #[test]
    fn workspace_selection_rejects_url_results_from_the_dialog() {
        let selection = "file:///private/tmp"
            .parse::<FilePath>()
            .expect("file URL must parse as a dialog result");

        assert_eq!(
            normalize_dialog_selection(Some(selection)).unwrap_err(),
            WORKSPACE_SELECTION_FAILED
        );
    }

    #[cfg(unix)]
    #[test]
    fn workspace_path_conversion_rejects_non_utf8_paths_generically() {
        use std::{ffi::OsString, os::unix::ffi::OsStringExt};

        let path = PathBuf::from(OsString::from_vec(vec![b'/', b'w', b'o', b'r', b'k', 0xff]));

        assert_eq!(
            workspace_path_to_utf8(path).unwrap_err(),
            WORKSPACE_SELECTION_FAILED
        );
    }

    struct FakeNotifications {
        permission: NativeNotificationPermission,
        requested: NativeNotificationPermission,
        sent: std::sync::Mutex<Vec<(String, String)>>,
    }

    impl NotificationAuthority for FakeNotifications {
        fn permission(&self) -> Result<NativeNotificationPermission, ()> {
            Ok(self.permission)
        }

        fn request_permission(&self) -> Result<NativeNotificationPermission, ()> {
            Ok(self.requested)
        }

        fn send(&self, title: &str, body: &str) -> Result<(), ()> {
            self.sent
                .lock()
                .expect("notification capture lock must not be poisoned")
                .push((title.to_owned(), body.to_owned()));
            Ok(())
        }
    }

    #[test]
    fn notification_caps_are_character_based_and_denial_is_generic() {
        let denied = FakeNotifications {
            permission: NativeNotificationPermission::Prompt,
            requested: NativeNotificationPermission::Denied,
            sent: std::sync::Mutex::new(Vec::new()),
        };
        let private_body = "private-body";
        let error = send_notification(&denied, "Agent", private_body).unwrap_err();
        assert_eq!(error, NOTIFICATION_UNAVAILABLE);
        assert!(!error.contains(private_body));

        let granted = FakeNotifications {
            permission: NativeNotificationPermission::Granted,
            requested: NativeNotificationPermission::Denied,
            sent: std::sync::Mutex::new(Vec::new()),
        };
        assert!(send_notification(&granted, &"界".repeat(TITLE_CHARACTER_LIMIT), "body").is_ok());
        assert_eq!(
            send_notification(&granted, &"界".repeat(TITLE_CHARACTER_LIMIT + 1), "body")
                .unwrap_err(),
            NOTIFICATION_UNAVAILABLE
        );
        assert_eq!(
            send_notification(&granted, "Agent", &"x".repeat(BODY_CHARACTER_LIMIT + 1))
                .unwrap_err(),
            NOTIFICATION_UNAVAILABLE
        );
    }

    #[test]
    fn notification_sends_only_plain_title_and_body_after_permission() {
        let granted = FakeNotifications {
            permission: NativeNotificationPermission::Prompt,
            requested: NativeNotificationPermission::Granted,
            sent: std::sync::Mutex::new(Vec::new()),
        };

        send_notification(&granted, "Completed", "The agent finished.")
            .expect("notification must be sent");

        assert_eq!(
            *granted.sent.lock().expect("capture lock must be available"),
            vec![("Completed".to_owned(), "The agent finished.".to_owned())]
        );
    }

    #[test]
    fn log_directory_is_created_and_returned_canonically_without_frontend_input() {
        let root = unique_temp_path("logs");
        let expected = root.join("app-log");

        let opened = prepare_log_directory(&expected).expect("log directory must be prepared");

        assert!(opened.is_absolute());
        assert_eq!(opened, expected.canonicalize().unwrap());
        fs::remove_dir_all(root).expect("temporary logs must be removed");
    }

    #[test]
    fn restart_result_preserves_a_valid_connection_but_never_lifecycle_detail() {
        let connection =
            ServiceConnection::new("http://127.0.0.1:49152".to_owned(), "r".repeat(43));
        assert!(restart_result(Ok(connection)).is_ok());

        let error = restart_result(Err(LifecycleError::RestartRefused))
            .err()
            .expect("restart refusal must fail");
        assert_eq!(error, RESTART_UNAVAILABLE);
        assert!(!error.contains("restart is not available"));
    }

    #[test]
    fn serialized_command_errors_never_contain_supplied_secret_path_or_body() {
        let supplied = [
            "s".repeat(43),
            "/private/workspace".to_owned(),
            "private body".to_owned(),
        ];
        let errors = [
            SERVICE_UNAVAILABLE,
            WORKSPACE_SELECTION_FAILED,
            NOTIFICATION_UNAVAILABLE,
            LOGS_UNAVAILABLE,
            RESTART_UNAVAILABLE,
        ];

        for error in errors {
            let serialized = serde_json::to_string(error).expect("command error must serialize");
            for private_value in &supplied {
                assert!(!serialized.contains(private_value));
            }
        }
    }
}
