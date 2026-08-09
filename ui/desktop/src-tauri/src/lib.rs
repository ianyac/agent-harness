pub mod lifecycle;
pub mod readiness;
pub mod state;

use tauri::Manager as _;

pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_notification::init())
        .plugin(tauri_plugin_opener::init())
        .setup(|app| {
            let diagnostic_log_path = app.path().app_log_dir()?.join("sidecar.log");
            app.manage(lifecycle::ServiceLifecycle::for_tauri(
                app.handle().clone(),
                diagnostic_log_path,
            ));
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("error while running Harness");
}

#[cfg(test)]
mod tests {
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
}
