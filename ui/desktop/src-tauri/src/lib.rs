pub mod commands;
pub mod lifecycle;
pub mod readiness;
pub mod state;

use std::{
    fs::{self, OpenOptions},
    future::Future,
    io::{self, Read as _, Write as _},
    path::{Path, PathBuf},
    sync::{
        Mutex as StdMutex,
        atomic::{AtomicU8, Ordering},
    },
    time::{Duration, Instant},
};

use serde::{Deserialize, Serialize};
use tauri::{
    Emitter as _, Manager as _, Runtime,
    menu::{Menu, MenuItemBuilder, SubmenuBuilder},
};

const BOOTSTRAP_DIRECTORY_NAME: &str = "service-bootstrap";
const EXIT_SHUTDOWN_TIMEOUT: Duration = Duration::from_secs(20);
const EXIT_SHUTDOWN_RETRY_INTERVAL: Duration = Duration::from_millis(25);
const MAIN_WINDOW_LABEL: &str = "main";
const NATIVE_MENU_EVENT: &str = "native-menu";
const WINDOW_GEOMETRY_FILE_NAME: &str = "window-state.json";
const WINDOW_GEOMETRY_MAX_BYTES: usize = 4 * 1024;
const WINDOW_IO_TIMEOUT: Duration = Duration::from_secs(2);
const MIN_WINDOW_WIDTH: u32 = 900;
const MIN_WINDOW_HEIGHT: u32 = 600;
const MAX_WINDOW_DIMENSION: u32 = 16_384;
const MIN_VISIBLE_WIDTH: i64 = 128;
const MIN_VISIBLE_HEIGHT: i64 = 96;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum NativeMenuAction {
    NewChat,
    CommandPalette,
    ToggleActivity,
    Settings,
}

impl NativeMenuAction {
    const fn id(self) -> &'static str {
        match self {
            Self::NewChat => "harness.new-chat",
            Self::CommandPalette => "harness.command-palette",
            Self::ToggleActivity => "harness.toggle-activity",
            Self::Settings => "harness.settings",
        }
    }

    const fn label(self) -> &'static str {
        match self {
            Self::NewChat => "New Chat",
            Self::CommandPalette => "Command Palette…",
            Self::ToggleActivity => "Toggle Activity",
            Self::Settings => "Settings…",
        }
    }

    const fn accelerator(self) -> &'static str {
        match self {
            Self::NewChat => "Cmd+N",
            Self::CommandPalette => "Cmd+K",
            Self::ToggleActivity => "Cmd+Shift+I",
            Self::Settings => "Cmd+,",
        }
    }

    fn from_id(id: &str) -> Option<Self> {
        match id {
            "harness.new-chat" => Some(Self::NewChat),
            "harness.command-palette" => Some(Self::CommandPalette),
            "harness.toggle-activity" => Some(Self::ToggleActivity),
            "harness.settings" => Some(Self::Settings),
            _ => None,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum NativeMenuRole {
    Separator,
    Hide,
    HideOthers,
    ShowAll,
    Quit,
    Undo,
    Redo,
    Cut,
    Copy,
    Paste,
    SelectAll,
    Minimize,
    Fullscreen,
    CloseWindow,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum NativeMenuEntry {
    Custom(NativeMenuAction),
    Predefined(NativeMenuRole),
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
struct NativeMenuSection {
    label: &'static str,
    entries: &'static [NativeMenuEntry],
}

const APPLICATION_MENU_ENTRIES: &[NativeMenuEntry] = &[
    NativeMenuEntry::Custom(NativeMenuAction::Settings),
    NativeMenuEntry::Predefined(NativeMenuRole::Separator),
    NativeMenuEntry::Predefined(NativeMenuRole::Hide),
    NativeMenuEntry::Predefined(NativeMenuRole::HideOthers),
    NativeMenuEntry::Predefined(NativeMenuRole::ShowAll),
    NativeMenuEntry::Predefined(NativeMenuRole::Separator),
    NativeMenuEntry::Predefined(NativeMenuRole::Quit),
];
const FILE_MENU_ENTRIES: &[NativeMenuEntry] = &[NativeMenuEntry::Custom(NativeMenuAction::NewChat)];
const EDIT_MENU_ENTRIES: &[NativeMenuEntry] = &[
    NativeMenuEntry::Predefined(NativeMenuRole::Undo),
    NativeMenuEntry::Predefined(NativeMenuRole::Redo),
    NativeMenuEntry::Predefined(NativeMenuRole::Separator),
    NativeMenuEntry::Predefined(NativeMenuRole::Cut),
    NativeMenuEntry::Predefined(NativeMenuRole::Copy),
    NativeMenuEntry::Predefined(NativeMenuRole::Paste),
    NativeMenuEntry::Predefined(NativeMenuRole::SelectAll),
];
const VIEW_MENU_ENTRIES: &[NativeMenuEntry] = &[
    NativeMenuEntry::Custom(NativeMenuAction::CommandPalette),
    NativeMenuEntry::Custom(NativeMenuAction::ToggleActivity),
];
const WINDOW_MENU_ENTRIES: &[NativeMenuEntry] = &[
    NativeMenuEntry::Predefined(NativeMenuRole::Minimize),
    NativeMenuEntry::Predefined(NativeMenuRole::Fullscreen),
    NativeMenuEntry::Predefined(NativeMenuRole::CloseWindow),
];
const NATIVE_MENU_SECTIONS: &[NativeMenuSection] = &[
    NativeMenuSection {
        label: "Harness",
        entries: APPLICATION_MENU_ENTRIES,
    },
    NativeMenuSection {
        label: "File",
        entries: FILE_MENU_ENTRIES,
    },
    NativeMenuSection {
        label: "Edit",
        entries: EDIT_MENU_ENTRIES,
    },
    NativeMenuSection {
        label: "View",
        entries: VIEW_MENU_ENTRIES,
    },
    NativeMenuSection {
        label: "Window",
        entries: WINDOW_MENU_ENTRIES,
    },
];

fn native_menu_sections() -> &'static [NativeMenuSection] {
    NATIVE_MENU_SECTIONS
}

fn native_menu_payload(id: &str) -> Option<&'static str> {
    NativeMenuAction::from_id(id).map(NativeMenuAction::id)
}

fn append_native_menu_entry<'m, R: Runtime, M: tauri::Manager<R>>(
    mut builder: SubmenuBuilder<'m, R, M>,
    manager: &'m M,
    entry: NativeMenuEntry,
) -> tauri::Result<SubmenuBuilder<'m, R, M>> {
    builder = match entry {
        NativeMenuEntry::Custom(action) => {
            let item = MenuItemBuilder::with_id(action.id(), action.label())
                .accelerator(action.accelerator())
                .build(manager)?;
            builder.item(&item)
        }
        NativeMenuEntry::Predefined(role) => match role {
            NativeMenuRole::Separator => builder.separator(),
            NativeMenuRole::Hide => builder.hide(),
            NativeMenuRole::HideOthers => builder.hide_others(),
            NativeMenuRole::ShowAll => builder.show_all(),
            NativeMenuRole::Quit => builder.quit(),
            NativeMenuRole::Undo => builder.undo(),
            NativeMenuRole::Redo => builder.redo(),
            NativeMenuRole::Cut => builder.cut(),
            NativeMenuRole::Copy => builder.copy(),
            NativeMenuRole::Paste => builder.paste(),
            NativeMenuRole::SelectAll => builder.select_all(),
            NativeMenuRole::Minimize => builder.minimize(),
            NativeMenuRole::Fullscreen => builder.fullscreen(),
            NativeMenuRole::CloseWindow => builder.close_window(),
        },
    };
    Ok(builder)
}

fn build_native_menu<R: Runtime>(app: &tauri::AppHandle<R>) -> tauri::Result<Menu<R>> {
    let menu = Menu::new(app)?;
    for section in native_menu_sections() {
        let mut submenu = SubmenuBuilder::new(app, section.label);
        for entry in section.entries {
            submenu = append_native_menu_entry(submenu, app, *entry)?;
        }
        menu.append(&submenu.build()?)?;
    }
    Ok(menu)
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct WindowGeometry {
    x: i32,
    y: i32,
    width: u32,
    height: u32,
}

#[derive(Debug, Clone, PartialEq, Eq)]
struct DisplayGeometry {
    x: i32,
    y: i32,
    width: u32,
    height: u32,
}

fn has_sane_dimensions(geometry: WindowGeometry) -> bool {
    geometry.width >= MIN_WINDOW_WIDTH
        && geometry.height >= MIN_WINDOW_HEIGHT
        && geometry.width <= MAX_WINDOW_DIMENSION
        && geometry.height <= MAX_WINDOW_DIMENSION
}

fn overlap_length(
    first_start: i64,
    first_length: i64,
    second_start: i64,
    second_length: i64,
) -> i64 {
    (first_start + first_length)
        .min(second_start + second_length)
        .saturating_sub(first_start.max(second_start))
}

fn window_geometry_is_restorable(geometry: WindowGeometry, displays: &[DisplayGeometry]) -> bool {
    has_sane_dimensions(geometry)
        && displays.iter().any(|display| {
            let visible_width = overlap_length(
                i64::from(geometry.x),
                i64::from(geometry.width),
                i64::from(display.x),
                i64::from(display.width),
            );
            let visible_height = overlap_length(
                i64::from(geometry.y),
                i64::from(geometry.height),
                i64::from(display.y),
                i64::from(display.height),
            );
            visible_width >= MIN_VISIBLE_WIDTH && visible_height >= MIN_VISIBLE_HEIGHT
        })
}

fn decode_window_geometry(bytes: &[u8]) -> Option<WindowGeometry> {
    if bytes.len() > WINDOW_GEOMETRY_MAX_BYTES {
        return None;
    }
    serde_json::from_slice::<WindowGeometry>(bytes)
        .ok()
        .filter(|geometry| has_sane_dimensions(*geometry))
}

fn window_geometry_path(config_dir: &Path) -> PathBuf {
    config_dir.join(WINDOW_GEOMETRY_FILE_NAME)
}

fn canonical_config_directory(config_dir: &Path) -> io::Result<PathBuf> {
    fs::create_dir_all(config_dir)?;
    config_dir.canonicalize()
}

fn load_window_geometry(config_dir: &Path) -> io::Result<Option<WindowGeometry>> {
    let canonical_dir = canonical_config_directory(config_dir)?;
    let path = window_geometry_path(&canonical_dir);
    let file = match fs::File::open(path) {
        Ok(file) => file,
        Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(None),
        Err(error) => return Err(error),
    };
    let mut bytes = Vec::with_capacity(WINDOW_GEOMETRY_MAX_BYTES.min(512));
    file.take((WINDOW_GEOMETRY_MAX_BYTES + 1) as u64)
        .read_to_end(&mut bytes)?;
    Ok(decode_window_geometry(&bytes))
}

fn persist_window_geometry(config_dir: &Path, geometry: WindowGeometry) -> io::Result<()> {
    if !has_sane_dimensions(geometry) {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "window geometry is invalid",
        ));
    }
    let canonical_dir = canonical_config_directory(config_dir)?;
    let destination = window_geometry_path(&canonical_dir);
    let temporary = canonical_dir.join(format!(
        ".{WINDOW_GEOMETRY_FILE_NAME}.{}-{}.tmp",
        std::process::id(),
        rand::random::<u64>()
    ));
    let write_result = (|| {
        let mut file = OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&temporary)?;
        serde_json::to_writer(&mut file, &geometry).map_err(io::Error::other)?;
        file.write_all(b"\n")?;
        file.sync_all()?;
        fs::rename(&temporary, destination)
    })();
    if write_result.is_err() {
        let _ = fs::remove_file(&temporary);
    }
    write_result
}

struct WindowPersistence {
    config_dir: PathBuf,
    geometry: StdMutex<Option<WindowGeometry>>,
}

impl WindowPersistence {
    fn new(config_dir: PathBuf) -> Self {
        Self {
            config_dir,
            geometry: StdMutex::new(None),
        }
    }

    fn record(&self, geometry: WindowGeometry) {
        if has_sane_dimensions(geometry) {
            *self
                .geometry
                .lock()
                .unwrap_or_else(std::sync::PoisonError::into_inner) = Some(geometry);
        }
    }

    fn snapshot(&self) -> Option<WindowGeometry> {
        *self
            .geometry
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
    }
}

fn record_window_geometry(
    persistence: Option<&WindowPersistence>,
    geometry: Option<WindowGeometry>,
) {
    if let Some(persistence) = persistence
        && let Some(geometry) = geometry
    {
        persistence.record(geometry);
    }
}

fn geometry_for_exit(
    current: Option<WindowGeometry>,
    stored: Option<WindowGeometry>,
) -> Option<WindowGeometry> {
    current.or(stored)
}

trait WindowSurface {
    fn displays(&self) -> Result<Vec<DisplayGeometry>, ()>;
    fn set_size(&self, width: u32, height: u32) -> Result<(), ()>;
    fn set_position(&self, x: i32, y: i32) -> Result<(), ()>;
    fn center(&self) -> Result<(), ()>;
    fn show(&self) -> Result<(), ()>;
    fn focus(&self) -> Result<(), ()>;
}

struct TauriWindowSurface<R: Runtime>(tauri::WebviewWindow<R>);

impl<R: Runtime> WindowSurface for TauriWindowSurface<R> {
    fn displays(&self) -> Result<Vec<DisplayGeometry>, ()> {
        self.0
            .available_monitors()
            .map(|monitors| {
                monitors
                    .into_iter()
                    .map(|monitor| {
                        let area = monitor.work_area();
                        DisplayGeometry {
                            x: area.position.x,
                            y: area.position.y,
                            width: area.size.width,
                            height: area.size.height,
                        }
                    })
                    .collect()
            })
            .map_err(|_| ())
    }

    fn set_size(&self, width: u32, height: u32) -> Result<(), ()> {
        self.0
            .set_size(tauri::PhysicalSize::new(width, height))
            .map_err(|_| ())
    }

    fn set_position(&self, x: i32, y: i32) -> Result<(), ()> {
        self.0
            .set_position(tauri::PhysicalPosition::new(x, y))
            .map_err(|_| ())
    }

    fn center(&self) -> Result<(), ()> {
        self.0.center().map_err(|_| ())
    }

    fn show(&self) -> Result<(), ()> {
        self.0.show().map_err(|_| ())
    }

    fn focus(&self) -> Result<(), ()> {
        self.0.set_focus().map_err(|_| ())
    }
}

fn restore_and_reveal(window: &impl WindowSurface, saved: Option<WindowGeometry>) {
    let restored = saved
        .zip(window.displays().ok())
        .filter(|(geometry, displays)| window_geometry_is_restorable(*geometry, displays))
        .is_some_and(|(geometry, _)| {
            window.set_size(geometry.width, geometry.height).is_ok()
                && window.set_position(geometry.x, geometry.y).is_ok()
        });
    if !restored {
        let _ = window.center();
    }
    let _ = window.show();
    let _ = window.focus();
}

async fn complete_startup<T, E>(
    startup: impl Future<Output = Result<T, E>>,
    reveal: impl Future<Output = ()>,
) -> Result<T, E> {
    let result = startup.await;
    reveal.await;
    result
}

async fn load_geometry_without_blocking(config_dir: PathBuf) -> Option<WindowGeometry> {
    let load = tauri::async_runtime::spawn_blocking(move || load_window_geometry(&config_dir));
    match tokio::time::timeout(WINDOW_IO_TIMEOUT, load).await {
        Ok(Ok(Ok(geometry))) => geometry,
        _ => None,
    }
}

async fn persist_geometry_without_blocking(config_dir: PathBuf, geometry: WindowGeometry) {
    let persist = tauri::async_runtime::spawn_blocking(move || {
        persist_window_geometry(&config_dir, geometry)
    });
    let _ = tokio::time::timeout(WINDOW_IO_TIMEOUT, persist).await;
}

fn current_window_geometry<R: Runtime>(window: &tauri::Window<R>) -> Option<WindowGeometry> {
    let position = window.outer_position().ok()?;
    let size = window.inner_size().ok()?;
    Some(WindowGeometry {
        x: position.x,
        y: position.y,
        width: size.width,
        height: size.height,
    })
    .filter(|geometry| has_sane_dimensions(*geometry))
}

fn current_webview_window_geometry<R: Runtime>(
    window: &tauri::WebviewWindow<R>,
) -> Option<WindowGeometry> {
    let position = window.outer_position().ok()?;
    let size = window.inner_size().ok()?;
    Some(WindowGeometry {
        x: position.x,
        y: position.y,
        width: size.width,
        height: size.height,
    })
    .filter(|geometry| has_sane_dimensions(*geometry))
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum ExitDecision {
    StartCleanup,
    Prevent,
    Allow,
}

#[repr(u8)]
enum ExitPhase {
    Idle,
    CleaningUp,
    Reissued,
}

#[derive(Default)]
struct ExitCoordinator {
    phase: AtomicU8,
}

impl ExitCoordinator {
    fn on_exit_requested(&self) -> ExitDecision {
        match self.phase.compare_exchange(
            ExitPhase::Idle as u8,
            ExitPhase::CleaningUp as u8,
            Ordering::AcqRel,
            Ordering::Acquire,
        ) {
            Ok(_) => ExitDecision::StartCleanup,
            Err(phase) if phase == ExitPhase::CleaningUp as u8 => ExitDecision::Prevent,
            Err(_) => ExitDecision::Allow,
        }
    }

    fn mark_reissued(&self) {
        self.phase
            .store(ExitPhase::Reissued as u8, Ordering::Release);
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

async fn complete_graceful_exit(
    persist: impl Future<Output = ()>,
    shutdown: impl Future<Output = ()>,
    reissue_exit: impl FnOnce(),
) {
    persist.await;
    shutdown.await;
    reissue_exit();
}

pub fn run() {
    let app = tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_notification::init())
        .plugin(tauri_plugin_opener::init())
        .menu(build_native_menu)
        .on_menu_event(|app, event| {
            if let Some(payload) = native_menu_payload(event.id().as_ref())
                && let Some(window) = app.get_webview_window(MAIN_WINDOW_LABEL)
            {
                let _ = window.emit(NATIVE_MENU_EVENT, payload);
            }
        })
        .on_window_event(|window, event| {
            if window.label() != MAIN_WINDOW_LABEL
                || !matches!(
                    event,
                    tauri::WindowEvent::Moved(_) | tauri::WindowEvent::Resized(_)
                )
            {
                return;
            }
            let persistence = window.app_handle().try_state::<WindowPersistence>();
            record_window_geometry(persistence.as_deref(), current_window_geometry(window));
        })
        .invoke_handler(commands::handler())
        .setup(|app| {
            let bootstrap_workspace = canonical_bootstrap_directory(&app.path().app_data_dir()?)?;
            let config_dir = app.path().app_config_dir()?;
            let diagnostic_log_path = app.path().app_log_dir()?.join("sidecar.log");
            let lifecycle =
                lifecycle::ServiceLifecycle::for_tauri(app.handle().clone(), diagnostic_log_path);
            let main_window = app.get_webview_window(MAIN_WINDOW_LABEL).ok_or_else(|| {
                io::Error::new(io::ErrorKind::NotFound, "main window is unavailable")
            })?;
            app.manage(lifecycle.clone());
            app.manage(ExitCoordinator::default());
            app.manage(WindowPersistence::new(config_dir.clone()));
            tauri::async_runtime::spawn(async move {
                let _ = complete_startup(lifecycle.start(bootstrap_workspace), async move {
                    let saved = load_geometry_without_blocking(config_dir).await;
                    restore_and_reveal(&TauriWindowSurface(main_window), saved);
                })
                .await;
            });
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while running Harness");
    app.run(|handle, event| {
        if let tauri::RunEvent::ExitRequested { api, .. } = event {
            let decision = handle.state::<ExitCoordinator>().on_exit_requested();
            if decision != ExitDecision::Allow {
                api.prevent_exit();
            }
            if decision == ExitDecision::StartCleanup {
                let handle = handle.clone();
                let persistence = handle.state::<WindowPersistence>();
                let current = handle
                    .get_webview_window(MAIN_WINDOW_LABEL)
                    .as_ref()
                    .and_then(current_webview_window_geometry);
                let geometry = geometry_for_exit(current, persistence.snapshot());
                let config_dir = persistence.config_dir.clone();
                let lifecycle = handle
                    .state::<lifecycle::ServiceLifecycle>()
                    .inner()
                    .clone();
                tauri::async_runtime::spawn(async move {
                    complete_graceful_exit(
                        async move {
                            if let Some(geometry) = geometry {
                                persist_geometry_without_blocking(config_dir, geometry).await;
                            }
                        },
                        shutdown_before_exit(&lifecycle),
                        || {
                            handle.state::<ExitCoordinator>().mark_reissued();
                            handle.exit(0);
                        },
                    )
                    .await;
                });
            }
        }
    });
}

#[cfg(test)]
mod tests {
    use std::{fs, io::Write as _, sync::Mutex};

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
    fn graceful_exit_prevents_repeated_requests_until_cleanup_reissues_once() {
        let coordinator = ExitCoordinator::default();

        assert_eq!(coordinator.on_exit_requested(), ExitDecision::StartCleanup);
        assert_eq!(coordinator.on_exit_requested(), ExitDecision::Prevent);
        assert_eq!(coordinator.on_exit_requested(), ExitDecision::Prevent);

        coordinator.mark_reissued();
        assert_eq!(coordinator.on_exit_requested(), ExitDecision::Allow);
    }

    #[test]
    fn native_geometry_capture_uses_the_same_inner_size_that_restore_sets() {
        let source = include_str!("lib.rs");
        let window_capture = source
            .split_once("fn current_window_geometry")
            .unwrap()
            .1
            .split_once("fn current_webview_window_geometry")
            .unwrap()
            .0;
        let webview_capture = source
            .split_once("fn current_webview_window_geometry")
            .unwrap()
            .1
            .split_once("#[derive(Debug, Clone, Copy, PartialEq, Eq)]\nenum ExitDecision")
            .unwrap()
            .0;

        for capture in [window_capture, webview_capture] {
            assert!(capture.contains("window.inner_size()"));
            assert!(!capture.contains("window.outer_size()"));
        }
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
                        .push("persist");
                },
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
                vec!["persist", "shutdown", "exit"]
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

    #[test]
    fn native_menu_contract_has_exact_custom_actions_and_native_hide_quit_roles() {
        let custom = native_menu_sections()
            .iter()
            .flat_map(|section| section.entries)
            .filter_map(|entry| match entry {
                NativeMenuEntry::Custom(action) => {
                    Some((action.id(), action.label(), action.accelerator()))
                }
                _ => None,
            })
            .collect::<Vec<_>>();

        assert_eq!(
            custom,
            vec![
                ("harness.settings", "Settings…", "Cmd+,"),
                ("harness.new-chat", "New Chat", "Cmd+N"),
                ("harness.command-palette", "Command Palette…", "Cmd+K",),
                ("harness.toggle-activity", "Toggle Activity", "Cmd+Shift+I",),
            ]
        );
        let app_entries = native_menu_sections()[0].entries;
        assert!(app_entries.contains(&NativeMenuEntry::Predefined(NativeMenuRole::Hide)));
        assert!(app_entries.contains(&NativeMenuEntry::Predefined(NativeMenuRole::Quit)));
    }

    #[test]
    fn menu_event_routing_emits_only_fixed_ids() {
        for expected in [
            "harness.new-chat",
            "harness.command-palette",
            "harness.toggle-activity",
            "harness.settings",
        ] {
            assert_eq!(native_menu_payload(expected), Some(expected));
        }
        assert_eq!(native_menu_payload("harness.run-arbitrary"), None);
        assert_eq!(native_menu_payload("quit"), None);
        assert_eq!(native_menu_payload(""), None);
    }

    fn geometry(x: i32, y: i32, width: u32, height: u32) -> WindowGeometry {
        WindowGeometry {
            x,
            y,
            width,
            height,
        }
    }

    fn display(x: i32, y: i32, width: u32, height: u32) -> DisplayGeometry {
        DisplayGeometry {
            x,
            y,
            width,
            height,
        }
    }

    #[test]
    fn window_geometry_accepts_a_usefully_visible_frame_on_negative_coordinate_display() {
        let displays = [display(0, 0, 1512, 982), display(-1920, -200, 1920, 1080)];

        assert!(window_geometry_is_restorable(
            geometry(-1800, -120, 1280, 800),
            &displays,
        ));
        assert!(window_geometry_is_restorable(
            geometry(-64, 120, 960, 640),
            &displays,
        ));
    }

    #[test]
    fn window_geometry_rejects_invalid_dimensions_and_fully_off_screen_frames() {
        let displays = [display(0, 0, 1512, 982)];

        assert!(!window_geometry_is_restorable(
            geometry(10, 10, 899, 600),
            &displays,
        ));
        assert!(!window_geometry_is_restorable(
            geometry(10, 10, 900, 599),
            &displays,
        ));
        assert!(!window_geometry_is_restorable(
            geometry(10, 10, 20_000, 800),
            &displays,
        ));
        assert!(!window_geometry_is_restorable(
            geometry(4_000, 4_000, 1280, 800),
            &displays,
        ));
        assert!(!window_geometry_is_restorable(
            geometry(1490, 960, 1280, 800),
            &displays,
        ));
    }

    #[test]
    fn geometry_json_is_strict_bounded_and_contains_only_physical_frame_fields() {
        let expected = geometry(-120, 40, 1280, 800);
        let encoded = serde_json::to_vec(&expected).unwrap();
        let keys = serde_json::from_slice::<serde_json::Value>(&encoded)
            .unwrap()
            .as_object()
            .unwrap()
            .keys()
            .cloned()
            .collect::<std::collections::BTreeSet<_>>();

        assert_eq!(
            keys,
            ["height", "width", "x", "y"]
                .into_iter()
                .map(str::to_owned)
                .collect()
        );
        assert_eq!(decode_window_geometry(&encoded), Some(expected));
        assert_eq!(decode_window_geometry(b"{broken"), None);
        assert_eq!(
            decode_window_geometry(br#"{"x":0,"y":0,"width":1280,"height":800,"token":"private"}"#),
            None
        );
        assert_eq!(
            decode_window_geometry(&vec![b' '; WINDOW_GEOMETRY_MAX_BYTES + 1]),
            None
        );
    }

    #[test]
    fn geometry_persistence_uses_one_fixed_app_config_file_and_replaces_atomically() {
        let root = std::env::temp_dir().join(format!(
            "agent-harness-window-state-{}-{}",
            std::process::id(),
            rand::random::<u64>()
        ));
        let first = geometry(20, 30, 1280, 800);
        let second = geometry(-900, 50, 1440, 900);

        persist_window_geometry(&root, first).unwrap();
        assert_eq!(load_window_geometry(&root).unwrap(), Some(first));
        persist_window_geometry(&root, second).unwrap();
        assert_eq!(load_window_geometry(&root).unwrap(), Some(second));
        let entries = fs::read_dir(&root)
            .unwrap()
            .map(|entry| entry.unwrap().file_name())
            .collect::<Vec<_>>();
        assert_eq!(entries, vec![std::ffi::OsString::from("window-state.json")]);
        assert_eq!(window_geometry_path(&root), root.join("window-state.json"));

        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn early_window_events_are_ignored_until_persistence_state_is_managed() {
        let candidate = geometry(40, 50, 1280, 800);
        record_window_geometry(None, Some(candidate));

        let persistence = WindowPersistence::new(PathBuf::from("/unused"));
        record_window_geometry(Some(&persistence), Some(candidate));
        assert_eq!(persistence.snapshot(), Some(candidate));
    }

    #[test]
    fn exit_geometry_prefers_current_frame_then_falls_back_to_memory_or_none() {
        let current = geometry(10, 20, 1280, 800);
        let stored = geometry(-900, 60, 1440, 900);

        assert_eq!(
            geometry_for_exit(Some(current), Some(stored)),
            Some(current)
        );
        assert_eq!(geometry_for_exit(None, Some(stored)), Some(stored));
        assert_eq!(geometry_for_exit(None, None), None);
    }

    #[test]
    fn graceful_exit_still_shuts_down_and_reissues_without_any_geometry() {
        tauri::async_runtime::block_on(async {
            let steps = Mutex::new(Vec::new());
            let geometry = geometry_for_exit(None, None);

            complete_graceful_exit(
                async {
                    if geometry.is_some() {
                        steps.lock().unwrap().push("persist");
                    }
                },
                async {
                    steps.lock().unwrap().push("shutdown");
                },
                || steps.lock().unwrap().push("exit"),
            )
            .await;

            assert_eq!(*steps.lock().unwrap(), vec!["shutdown", "exit"]);
        });
    }

    #[test]
    fn geometry_loading_ignores_corrupt_and_oversized_files() {
        let root = std::env::temp_dir().join(format!(
            "agent-harness-window-state-invalid-{}-{}",
            std::process::id(),
            rand::random::<u64>()
        ));
        fs::create_dir_all(&root).unwrap();
        let path = root.join("window-state.json");
        fs::write(&path, b"not-json").unwrap();
        assert_eq!(load_window_geometry(&root).unwrap(), None);

        let mut file = fs::File::create(&path).unwrap();
        file.write_all(&vec![b'x'; WINDOW_GEOMETRY_MAX_BYTES + 1])
            .unwrap();
        drop(file);
        assert_eq!(load_window_geometry(&root).unwrap(), None);

        fs::remove_dir_all(root).unwrap();
    }

    #[derive(Default)]
    struct FakeWindowSurface {
        displays: Vec<DisplayGeometry>,
        calls: Mutex<Vec<&'static str>>,
    }

    impl WindowSurface for FakeWindowSurface {
        fn displays(&self) -> Result<Vec<DisplayGeometry>, ()> {
            Ok(self.displays.clone())
        }

        fn set_size(&self, _width: u32, _height: u32) -> Result<(), ()> {
            self.calls.lock().unwrap().push("size");
            Ok(())
        }

        fn set_position(&self, _x: i32, _y: i32) -> Result<(), ()> {
            self.calls.lock().unwrap().push("position");
            Ok(())
        }

        fn center(&self) -> Result<(), ()> {
            self.calls.lock().unwrap().push("center");
            Ok(())
        }

        fn show(&self) -> Result<(), ()> {
            self.calls.lock().unwrap().push("show");
            Ok(())
        }

        fn focus(&self) -> Result<(), ()> {
            self.calls.lock().unwrap().push("focus");
            Ok(())
        }
    }

    #[test]
    fn reveal_restores_before_show_and_centers_invalid_state() {
        let restored = FakeWindowSurface {
            displays: vec![display(0, 0, 1512, 982)],
            ..Default::default()
        };
        restore_and_reveal(&restored, Some(geometry(40, 50, 1280, 800)));
        assert_eq!(
            *restored.calls.lock().unwrap(),
            vec!["size", "position", "show", "focus"]
        );

        let centered = FakeWindowSurface {
            displays: vec![display(0, 0, 1512, 982)],
            ..Default::default()
        };
        restore_and_reveal(&centered, Some(geometry(8_000, 8_000, 1280, 800)));
        assert_eq!(
            *centered.calls.lock().unwrap(),
            vec!["center", "show", "focus"]
        );
    }

    #[test]
    fn startup_reveals_after_both_ready_and_failure_results() {
        tauri::async_runtime::block_on(async {
            for succeeds in [true, false] {
                let steps = Mutex::new(Vec::new());
                let result: Result<(), &'static str> = complete_startup(
                    async {
                        steps.lock().unwrap().push("startup");
                        if succeeds { Ok(()) } else { Err("failed") }
                    },
                    async {
                        steps.lock().unwrap().push("reveal");
                    },
                )
                .await;

                assert_eq!(result.is_ok(), succeeds);
                assert_eq!(*steps.lock().unwrap(), vec!["startup", "reveal"]);
            }
        });
    }

    fn png_dimensions_and_alpha(path: &Path) -> (u32, u32, bool) {
        let bytes = fs::read(path).unwrap();
        assert_eq!(&bytes[..8], b"\x89PNG\r\n\x1a\n");
        assert_eq!(&bytes[12..16], b"IHDR");
        let width = u32::from_be_bytes(bytes[16..20].try_into().unwrap());
        let height = u32::from_be_bytes(bytes[20..24].try_into().unwrap());
        let has_alpha = matches!(bytes[25], 4 | 6);
        (width, height, has_alpha)
    }

    #[test]
    fn generated_icon_contract_and_hidden_overlay_window_config_are_exact() {
        let manifest = Path::new(env!("CARGO_MANIFEST_DIR"));
        let source = manifest.join("../app-icon-source.png");
        assert_eq!(png_dimensions_and_alpha(&source), (1024, 1024, true));

        for (name, expected_size) in [
            ("32x32.png", 32),
            ("128x128.png", 128),
            ("128x128@2x.png", 256),
            ("icon.png", 512),
        ] {
            assert_eq!(
                png_dimensions_and_alpha(&manifest.join("icons").join(name)),
                (expected_size, expected_size, true),
            );
        }
        assert!(manifest.join("icons/icon.icns").is_file());

        let config: serde_json::Value =
            serde_json::from_str(include_str!("../tauri.conf.json")).unwrap();
        let window = &config["app"]["windows"][0];
        assert_eq!(window["label"], "main");
        assert_eq!(window["visible"], false);
        assert_eq!(window["decorations"], true);
        assert_eq!(window["titleBarStyle"], "Overlay");
        assert_eq!(window["hiddenTitle"], true);
        assert!(window.get("trafficLightPosition").is_none());
        assert_eq!(
            config["bundle"]["icon"],
            serde_json::json!([
                "icons/32x32.png",
                "icons/128x128.png",
                "icons/128x128@2x.png",
                "icons/icon.icns",
                "icons/icon.ico"
            ])
        );
        for icon in config["bundle"]["icon"].as_array().unwrap() {
            assert!(manifest.join(icon.as_str().unwrap()).is_file());
        }
    }
}
