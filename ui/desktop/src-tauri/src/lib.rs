pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_notification::init())
        .plugin(tauri_plugin_opener::init())
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
            config.get("mainBinaryName").and_then(|value| value.as_str()),
            Some("Harness")
        );
    }
}
