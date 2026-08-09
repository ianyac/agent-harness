use std::{
    fmt,
    future::{Future, poll_fn},
    io::{Read, Write},
    path::{Path, PathBuf},
    pin::Pin,
    process::Child,
    sync::{Arc, Mutex as StdMutex},
    task::Poll,
    thread,
    time::Duration,
};

use base64::Engine as _;
use rand::RngCore as _;
use serde::Serialize;
use tauri::{AppHandle, Emitter as _, Runtime};
use tauri_plugin_shell::ShellExt as _;
use tokio::sync::{Mutex, watch};

use crate::{
    readiness::{LOOPBACK_HOST, ReadinessAccumulator},
    state::{LifecycleStatus, ServiceConnection, ServiceStateEvent},
};

pub const READINESS_TIMEOUT: Duration = Duration::from_secs(15);
pub const PROCESS_EXIT_TIMEOUT: Duration = Duration::from_secs(2);
pub const SIDECAR_NAME: &str = "agent-harness-sidecar";

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum LifecycleError {
    InvalidWorkspace,
    AlreadyStarted,
    Busy,
    StartupFailed,
    StartupTerminated,
    ReadinessFailed,
    ReadinessTimedOut,
    ProcessControlFailed,
    RestartRefused,
}

impl fmt::Display for LifecycleError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        let message = match self {
            Self::InvalidWorkspace => "workspace is not a canonical directory",
            Self::AlreadyStarted => "service is already started",
            Self::Busy => "service lifecycle operation is already in progress",
            Self::StartupFailed => "service startup failed",
            Self::StartupTerminated => "service terminated during startup",
            Self::ReadinessFailed => "service readiness failed",
            Self::ReadinessTimedOut => "service readiness timed out",
            Self::ProcessControlFailed => "service process control failed",
            Self::RestartRefused => "service restart is not available",
        };
        formatter.write_str(message)
    }
}

impl std::error::Error for LifecycleError {}

pub enum ProcessEvent {
    Stdout(Vec<u8>),
    Stderr(Vec<u8>),
    OutputError,
    Error,
    Terminated,
}

pub trait SidecarChild: Send {
    fn kill(&mut self) -> Result<(), LifecycleError>;
}

pub trait SidecarEventStream: Send {
    fn next_event(&mut self) -> Pin<Box<dyn Future<Output = Option<ProcessEvent>> + Send + '_>>;
}

pub struct SpawnedSidecar {
    child: Box<dyn SidecarChild>,
    events: Box<dyn SidecarEventStream>,
}

impl SpawnedSidecar {
    pub fn new(child: Box<dyn SidecarChild>, events: Box<dyn SidecarEventStream>) -> Self {
        Self { child, events }
    }
}

pub struct BootstrapInput {
    bytes: Vec<u8>,
}

impl BootstrapInput {
    pub fn as_bytes(&self) -> &[u8] {
        &self.bytes
    }

    pub fn into_bytes(self) -> Vec<u8> {
        self.bytes
    }
}

pub trait SidecarSpawner: Send + Sync {
    fn spawn(&self, bootstrap: BootstrapInput) -> Result<SpawnedSidecar, LifecycleError>;
}

pub trait TokenSource: Send + Sync {
    fn generate(&self) -> String;
}

pub trait ReadinessTimer: Send + Sync {
    fn wait(&self, duration: Duration) -> Pin<Box<dyn Future<Output = ()> + Send>>;
}

pub trait LifecycleEventSink: Send + Sync {
    fn emit(&self, event: ServiceStateEvent);
}

pub struct OsTokenSource;

impl TokenSource for OsTokenSource {
    fn generate(&self) -> String {
        generate_token()
    }
}

pub struct TokioReadinessTimer;

impl ReadinessTimer for TokioReadinessTimer {
    fn wait(&self, duration: Duration) -> Pin<Box<dyn Future<Output = ()> + Send>> {
        Box::pin(tokio::time::sleep(duration))
    }
}

pub struct TauriSidecarSpawner<R: Runtime> {
    app: AppHandle<R>,
}

impl<R: Runtime> TauriSidecarSpawner<R> {
    pub fn new(app: AppHandle<R>) -> Self {
        Self { app }
    }

    pub fn program_name() -> &'static str {
        SIDECAR_NAME
    }
}

struct TauriSidecarChild {
    child: Arc<StdMutex<Child>>,
}

impl SidecarChild for TauriSidecarChild {
    fn kill(&mut self) -> Result<(), LifecycleError> {
        let mut child = self
            .child
            .lock()
            .map_err(|_| LifecycleError::ProcessControlFailed)?;
        match child
            .try_wait()
            .map_err(|_| LifecycleError::ProcessControlFailed)?
        {
            Some(_) => Ok(()),
            None => child
                .kill()
                .map_err(|_| LifecycleError::ProcessControlFailed),
        }
    }
}

impl Drop for TauriSidecarChild {
    fn drop(&mut self) {
        if let Ok(mut child) = self.child.lock()
            && child.try_wait().ok().flatten().is_none()
        {
            let _ = child.kill();
        }
    }
}

struct TauriSidecarEventStream {
    receiver: tokio::sync::mpsc::UnboundedReceiver<ProcessEvent>,
}

impl SidecarEventStream for TauriSidecarEventStream {
    fn next_event(&mut self) -> Pin<Box<dyn Future<Output = Option<ProcessEvent>> + Send + '_>> {
        Box::pin(self.receiver.recv())
    }
}

impl<R: Runtime> SidecarSpawner for TauriSidecarSpawner<R> {
    fn spawn(&self, bootstrap: BootstrapInput) -> Result<SpawnedSidecar, LifecycleError> {
        let command = self
            .app
            .shell()
            .sidecar(SIDECAR_NAME)
            .map_err(|_| LifecycleError::StartupFailed)?;
        let mut command: std::process::Command = command.into();
        validate_sidecar_command_transport(&command)?;
        let mut child = command.spawn().map_err(|_| LifecycleError::StartupFailed)?;
        let stdin = child.stdin.take().ok_or_else(|| {
            terminate_unmanaged_child(&mut child);
            LifecycleError::StartupFailed
        })?;
        if write_bootstrap_and_close(stdin, &bootstrap).is_err() {
            terminate_unmanaged_child(&mut child);
            return Err(LifecycleError::StartupFailed);
        }
        let stdout = child.stdout.take().ok_or_else(|| {
            terminate_unmanaged_child(&mut child);
            LifecycleError::StartupFailed
        })?;
        let stderr = child.stderr.take().ok_or_else(|| {
            terminate_unmanaged_child(&mut child);
            LifecycleError::StartupFailed
        })?;
        let child = Arc::new(StdMutex::new(child));
        let (sender, receiver) = tokio::sync::mpsc::unbounded_channel();
        spawn_output_reader(stdout, sender.clone(), ProcessEvent::Stdout);
        spawn_output_reader(stderr, sender.clone(), ProcessEvent::Stderr);
        spawn_process_waiter(Arc::clone(&child), sender);
        Ok(SpawnedSidecar::new(
            Box::new(TauriSidecarChild { child }),
            Box::new(TauriSidecarEventStream { receiver }),
        ))
    }
}

fn write_bootstrap_and_close(
    mut stdin: impl Write,
    bootstrap: &BootstrapInput,
) -> Result<(), LifecycleError> {
    stdin
        .write_all(bootstrap.as_bytes())
        .and_then(|()| stdin.flush())
        .map_err(|_| LifecycleError::StartupFailed)
}

fn validate_sidecar_command_transport(
    command: &std::process::Command,
) -> Result<(), LifecycleError> {
    if command.get_args().next().is_some() || command.get_envs().next().is_some() {
        return Err(LifecycleError::StartupFailed);
    }
    Ok(())
}

fn terminate_unmanaged_child(child: &mut Child) {
    if child.try_wait().ok().flatten().is_none() {
        let _ = child.kill();
    }
    let _ = child.wait();
}

fn spawn_output_reader(
    mut reader: impl Read + Send + 'static,
    sender: tokio::sync::mpsc::UnboundedSender<ProcessEvent>,
    wrap: fn(Vec<u8>) -> ProcessEvent,
) {
    thread::spawn(move || {
        let mut buffer = [0_u8; 4_096];
        loop {
            match reader.read(&mut buffer) {
                Ok(0) => return,
                Ok(length) => {
                    if sender.send(wrap(buffer[..length].to_vec())).is_err() {
                        return;
                    }
                }
                Err(_) => {
                    let _ = sender.send(ProcessEvent::OutputError);
                    return;
                }
            }
        }
    });
}

fn spawn_process_waiter(
    child: Arc<StdMutex<Child>>,
    sender: tokio::sync::mpsc::UnboundedSender<ProcessEvent>,
) {
    thread::spawn(move || {
        loop {
            let result = match child.lock() {
                Ok(mut child) => child.try_wait(),
                Err(poisoned) => poisoned.into_inner().try_wait(),
            };
            match result {
                Ok(Some(_)) => {
                    let _ = sender.send(ProcessEvent::Terminated);
                    return;
                }
                Ok(None) => thread::sleep(Duration::from_millis(10)),
                Err(_) => {
                    let _ = sender.send(ProcessEvent::Error);
                    thread::sleep(Duration::from_millis(10));
                }
            }
        }
    });
}

pub struct TauriLifecycleEventSink<R: Runtime> {
    app: AppHandle<R>,
}

impl<R: Runtime> TauriLifecycleEventSink<R> {
    pub fn new(app: AppHandle<R>) -> Self {
        Self { app }
    }
}

impl<R: Runtime> LifecycleEventSink for TauriLifecycleEventSink<R> {
    fn emit(&self, event: ServiceStateEvent) {
        let _ = self.app.emit(crate::state::SERVICE_STATE_EVENT, event);
    }
}

#[derive(Serialize)]
struct BootstrapDocument<'a> {
    #[serde(rename = "type")]
    record_type: &'static str,
    secret: &'a str,
    workspace: &'a str,
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum Operation {
    Starting,
    Restarting,
    ShuttingDown,
}

struct ActiveProcess {
    generation: u64,
    child: Box<dyn SidecarChild>,
    termination: watch::Receiver<bool>,
}

struct LifecycleState {
    status: LifecycleStatus,
    operation: Option<Operation>,
    generation: u64,
    restart_count: u8,
    intentional_exit: bool,
    workspace: Option<PathBuf>,
    connection: Option<ServiceConnection>,
    active: Option<ActiveProcess>,
    diagnostic_log_path: PathBuf,
}

struct LifecycleInner {
    state: Mutex<LifecycleState>,
    spawner: Arc<dyn SidecarSpawner>,
    tokens: Arc<dyn TokenSource>,
    timer: Arc<dyn ReadinessTimer>,
    event_sink: Arc<dyn LifecycleEventSink>,
}

#[derive(Clone)]
pub struct ServiceLifecycle {
    inner: Arc<LifecycleInner>,
}

impl ServiceLifecycle {
    pub fn new(
        spawner: Arc<dyn SidecarSpawner>,
        tokens: Arc<dyn TokenSource>,
        timer: Arc<dyn ReadinessTimer>,
        event_sink: Arc<dyn LifecycleEventSink>,
        diagnostic_log_path: PathBuf,
    ) -> Self {
        Self {
            inner: Arc::new(LifecycleInner {
                state: Mutex::new(LifecycleState {
                    status: LifecycleStatus::Stopped,
                    operation: None,
                    generation: 0,
                    restart_count: 0,
                    intentional_exit: false,
                    workspace: None,
                    connection: None,
                    active: None,
                    diagnostic_log_path,
                }),
                spawner,
                tokens,
                timer,
                event_sink,
            }),
        }
    }

    pub fn for_tauri<R: Runtime>(app: AppHandle<R>, diagnostic_log_path: PathBuf) -> Self {
        Self::new(
            Arc::new(TauriSidecarSpawner::new(app.clone())),
            Arc::new(OsTokenSource),
            Arc::new(TokioReadinessTimer),
            Arc::new(TauriLifecycleEventSink::new(app)),
            diagnostic_log_path,
        )
    }

    pub async fn start(&self, workspace: PathBuf) -> Result<ServiceConnection, LifecycleError> {
        validate_workspace_encoding(&workspace)?;
        validate_canonical_workspace(&workspace)?;
        let generation = {
            let mut state = self.inner.state.lock().await;
            if state.operation.is_some() {
                return Err(LifecycleError::Busy);
            }
            if state.status != LifecycleStatus::Stopped {
                return Err(LifecycleError::AlreadyStarted);
            }
            state.generation = state.generation.saturating_add(1);
            state.status = LifecycleStatus::Starting;
            state.operation = Some(Operation::Starting);
            state.intentional_exit = false;
            state.workspace = Some(workspace.clone());
            state.connection = None;
            state.generation
        };
        self.inner.event_sink.emit(ServiceStateEvent::transition(
            generation,
            LifecycleStatus::Starting,
        ));
        self.launch_reserved(generation, workspace).await
    }

    pub async fn connection(&self) -> Option<ServiceConnection> {
        self.inner.state.lock().await.connection.clone()
    }

    pub async fn status(&self) -> LifecycleStatus {
        self.inner.state.lock().await.status
    }

    pub async fn diagnostic_log_path(&self) -> PathBuf {
        self.inner.state.lock().await.diagnostic_log_path.clone()
    }

    pub async fn restart_once(&self) -> Result<ServiceConnection, LifecycleError> {
        let (generation, workspace) = {
            let mut state = self.inner.state.lock().await;
            if state.operation.is_some() {
                return Err(LifecycleError::Busy);
            }
            if state.status != LifecycleStatus::Failed
                || state.restart_count >= 1
                || state.active.is_some()
            {
                return Err(LifecycleError::RestartRefused);
            }
            let workspace = state
                .workspace
                .clone()
                .ok_or(LifecycleError::RestartRefused)?;
            state.restart_count = 1;
            state.generation = state.generation.saturating_add(1);
            state.status = LifecycleStatus::Restarting;
            state.operation = Some(Operation::Restarting);
            state.intentional_exit = false;
            let generation = state.generation;
            (generation, workspace)
        };
        self.inner.event_sink.emit(ServiceStateEvent::transition(
            generation,
            LifecycleStatus::Restarting,
        ));
        self.launch_reserved(generation, workspace).await
    }

    #[cfg(test)]
    async fn restart_count(&self) -> u8 {
        self.inner.state.lock().await.restart_count
    }

    #[cfg(test)]
    async fn has_restart_workspace(&self) -> bool {
        self.inner.state.lock().await.workspace.is_some()
    }

    #[cfg(test)]
    async fn has_active_process(&self) -> bool {
        self.inner.state.lock().await.active.is_some()
    }

    pub async fn shutdown(&self) -> Result<(), LifecycleError> {
        let (generation, active) = {
            let mut state = self.inner.state.lock().await;
            if state.operation.is_some() {
                return Err(LifecycleError::Busy);
            }
            if state.status == LifecycleStatus::Stopped {
                return Ok(());
            }
            if state.status == LifecycleStatus::Failed && state.active.is_none() {
                state.status = LifecycleStatus::Stopped;
                state.workspace = None;
                state.connection = None;
                return Ok(());
            }
            let generation = state.generation;
            let active = state.active.take().ok_or(LifecycleError::Busy)?;
            if active.generation != generation {
                state.status = LifecycleStatus::Failed;
                state.operation = None;
                state.intentional_exit = false;
                drop(state);
                drop(active);
                return Err(LifecycleError::ProcessControlFailed);
            }
            state.intentional_exit = true;
            state.status = LifecycleStatus::Stopping;
            state.operation = Some(Operation::ShuttingDown);
            state.connection = None;
            (generation, active)
        };

        self.inner.event_sink.emit(ServiceStateEvent::transition(
            generation,
            LifecycleStatus::Stopping,
        ));
        let mut active = Some(active);
        let active_process = active.as_mut().expect("active process must be reserved");
        let kill_result = active_process.child.kill();
        let terminated = if kill_result.is_ok() {
            wait_for_termination_or_timeout(&mut active_process.termination, &*self.inner.timer)
                .await
        } else {
            false
        };
        let control_failed = kill_result.is_err() || !terminated;

        let (event, displaced) = {
            let mut state = self.inner.state.lock().await;
            if state.generation == generation && state.status == LifecycleStatus::Stopping {
                if control_failed {
                    state.status = LifecycleStatus::Failed;
                    state.operation = None;
                    state.connection = None;
                    let displaced = state
                        .active
                        .replace(active.take().expect("failed shutdown retains ownership"));
                    (
                        Some(ServiceStateEvent::transition(
                            generation,
                            LifecycleStatus::Failed,
                        )),
                        displaced,
                    )
                } else {
                    state.status = LifecycleStatus::Stopped;
                    state.operation = None;
                    state.intentional_exit = false;
                    state.workspace = None;
                    (
                        Some(ServiceStateEvent::transition(
                            generation,
                            LifecycleStatus::Stopped,
                        )),
                        None,
                    )
                }
            } else {
                (None, None)
            }
        };
        drop(displaced);
        drop(active);
        if let Some(event) = event {
            self.inner.event_sink.emit(event);
        }
        if control_failed {
            Err(LifecycleError::ProcessControlFailed)
        } else {
            Ok(())
        }
    }

    fn launch_reserved(
        &self,
        generation: u64,
        workspace: PathBuf,
    ) -> Pin<Box<dyn Future<Output = Result<ServiceConnection, LifecycleError>> + Send + '_>> {
        Box::pin(async move {
            let token = self.inner.tokens.generate();
            let bootstrap = match build_bootstrap(&workspace, &token) {
                Ok(bootstrap) => bootstrap,
                Err(error) => {
                    self.finish_start_failure(generation).await;
                    return Err(error);
                }
            };
            let SpawnedSidecar { child, mut events } = match self.inner.spawner.spawn(bootstrap) {
                Ok(spawned) => spawned,
                Err(error) => {
                    self.finish_start_failure(generation).await;
                    return Err(error);
                }
            };

            let mut accumulator = ReadinessAccumulator::new();
            let mut timeout = self.inner.timer.wait(READINESS_TIMEOUT);
            let ready = loop {
                let event = match wait_for_event_or_timeout(events.next_event(), &mut timeout).await
                {
                    ReadinessWait::Event(event) => event,
                    ReadinessWait::TimedOut => {
                        self.cleanup_failed_launch(generation, child, events).await;
                        return Err(LifecycleError::ReadinessTimedOut);
                    }
                };
                match event {
                    Some(ProcessEvent::Stdout(chunk)) => match accumulator.push(&chunk) {
                        Ok(Some(record)) => break record,
                        Ok(None) => {}
                        Err(_) => {
                            self.cleanup_failed_launch(generation, child, events).await;
                            return Err(LifecycleError::ReadinessFailed);
                        }
                    },
                    Some(ProcessEvent::Stderr(_)) => {}
                    Some(ProcessEvent::OutputError | ProcessEvent::Error) => {
                        self.cleanup_failed_launch(generation, child, events).await;
                        return Err(LifecycleError::StartupFailed);
                    }
                    Some(ProcessEvent::Terminated) | None => {
                        self.finish_start_failure(generation).await;
                        return Err(LifecycleError::StartupTerminated);
                    }
                }
            };

            let connection =
                ServiceConnection::new(format!("http://{LOOPBACK_HOST}:{}", ready.port()), token);
            let (termination_sender, termination) = watch::channel(false);
            {
                let mut state = self.inner.state.lock().await;
                if state.generation != generation
                    || !matches!(
                        state.operation,
                        Some(Operation::Starting | Operation::Restarting)
                    )
                    || state.active.is_some()
                {
                    drop(state);
                    self.cleanup_failed_launch(generation, child, events).await;
                    return Err(LifecycleError::Busy);
                }
                state.status = LifecycleStatus::Ready;
                state.operation = None;
                state.connection = Some(connection.clone());
                state.active = Some(ActiveProcess {
                    generation,
                    child,
                    termination,
                });
            }

            self.inner
                .event_sink
                .emit(ServiceStateEvent::connected(generation, connection.clone()));
            let lifecycle = self.clone();
            tauri::async_runtime::spawn(async move {
                lifecycle
                    .monitor_process(generation, events, termination_sender)
                    .await;
            });
            Ok(connection)
        })
    }

    async fn cleanup_failed_launch(
        &self,
        generation: u64,
        mut child: Box<dyn SidecarChild>,
        mut events: Box<dyn SidecarEventStream>,
    ) {
        if stop_startup_process(&mut *child, &mut *events, &*self.inner.timer).await {
            self.finish_start_failure(generation).await;
            drop(events);
            drop(child);
        } else {
            self.finish_start_failure_with_active(generation, child, events)
                .await;
        }
    }

    async fn finish_start_failure(&self, generation: u64) {
        let (event, retired) = {
            let mut state = self.inner.state.lock().await;
            if state.generation != generation {
                (None, None)
            } else {
                state.status = LifecycleStatus::Failed;
                state.operation = None;
                state.connection = None;
                if state.restart_count >= 1 {
                    state.workspace = None;
                }
                (
                    Some(ServiceStateEvent::transition(
                        generation,
                        LifecycleStatus::Failed,
                    )),
                    state.active.take(),
                )
            }
        };
        drop(retired);
        if let Some(event) = event {
            self.inner.event_sink.emit(event);
        }
    }

    async fn finish_start_failure_with_active(
        &self,
        generation: u64,
        child: Box<dyn SidecarChild>,
        events: Box<dyn SidecarEventStream>,
    ) {
        let (termination_sender, termination) = watch::channel(false);
        let mut child = Some(child);
        let mut events = Some(events);
        let (event, installed) = {
            let mut state = self.inner.state.lock().await;
            if state.generation != generation || state.active.is_some() {
                (None, false)
            } else {
                state.status = LifecycleStatus::Failed;
                state.operation = None;
                state.connection = None;
                if state.restart_count >= 1 {
                    state.workspace = None;
                }
                state.active = Some(ActiveProcess {
                    generation,
                    child: child
                        .take()
                        .expect("failed launch must retain its owned child"),
                    termination,
                });
                (
                    Some(ServiceStateEvent::transition(
                        generation,
                        LifecycleStatus::Failed,
                    )),
                    true,
                )
            }
        };
        if let Some(event) = event {
            self.inner.event_sink.emit(event);
        }
        if installed {
            let lifecycle = self.clone();
            let events = events
                .take()
                .expect("retained failed launch must keep its event stream");
            tauri::async_runtime::spawn(async move {
                lifecycle
                    .monitor_failed_cleanup(generation, events, termination_sender)
                    .await;
            });
        }
        drop(events);
        drop(child);
    }

    async fn monitor_process(
        &self,
        generation: u64,
        mut events: Box<dyn SidecarEventStream>,
        termination_sender: watch::Sender<bool>,
    ) {
        while let Some(event) = events.next_event().await {
            match event {
                ProcessEvent::Terminated => {
                    self.handle_process_end(generation).await;
                    let _ = termination_sender.send(true);
                    return;
                }
                ProcessEvent::Stdout(_)
                | ProcessEvent::Stderr(_)
                | ProcessEvent::OutputError
                | ProcessEvent::Error => {}
            }
        }
        self.mark_process_observation_failed(generation).await;
    }

    async fn monitor_failed_cleanup(
        &self,
        generation: u64,
        mut events: Box<dyn SidecarEventStream>,
        termination_sender: watch::Sender<bool>,
    ) {
        while let Some(event) = events.next_event().await {
            if matches!(event, ProcessEvent::Terminated) {
                let _ = termination_sender.send(true);
                self.handle_failed_cleanup_end(generation).await;
                return;
            }
        }
    }

    async fn handle_failed_cleanup_end(&self, generation: u64) {
        let retired = {
            let mut state = self.inner.state.lock().await;
            if state.generation != generation {
                None
            } else {
                state
                    .active
                    .take_if(|active| active.generation == generation)
            }
        };
        drop(retired);
    }

    async fn mark_process_observation_failed(&self, generation: u64) {
        let event = {
            let mut state = self.inner.state.lock().await;
            if state.generation != generation || state.active.is_none() {
                None
            } else {
                state.status = LifecycleStatus::Failed;
                state.operation = None;
                state.connection = None;
                Some(ServiceStateEvent::transition(
                    generation,
                    LifecycleStatus::Failed,
                ))
            }
        };
        if let Some(event) = event {
            self.inner.event_sink.emit(event);
        }
    }

    async fn handle_process_end(&self, generation: u64) {
        let (retired, event, restart) = {
            let mut state = self.inner.state.lock().await;
            if state.generation != generation {
                return;
            }
            if state.status == LifecycleStatus::Stopped && state.active.is_none() {
                return;
            }
            if state
                .active
                .as_ref()
                .is_some_and(|active| active.generation != generation)
            {
                return;
            }
            let retired = state.active.take();
            state.connection = None;
            if state.intentional_exit {
                state.status = LifecycleStatus::Stopped;
                state.operation = None;
                state.intentional_exit = false;
                state.workspace = None;
                (
                    retired,
                    ServiceStateEvent::transition(generation, LifecycleStatus::Stopped),
                    None,
                )
            } else if state.restart_count == 0 {
                if let Some(workspace) = state.workspace.clone() {
                    state.restart_count = 1;
                    state.generation = state.generation.saturating_add(1);
                    state.status = LifecycleStatus::Restarting;
                    state.operation = Some(Operation::Restarting);
                    let replacement_generation = state.generation;
                    (
                        retired,
                        ServiceStateEvent::transition(
                            replacement_generation,
                            LifecycleStatus::Restarting,
                        ),
                        Some((replacement_generation, workspace)),
                    )
                } else {
                    state.status = LifecycleStatus::Failed;
                    state.operation = None;
                    (
                        retired,
                        ServiceStateEvent::transition(generation, LifecycleStatus::Failed),
                        None,
                    )
                }
            } else {
                state.status = LifecycleStatus::Failed;
                state.operation = None;
                state.workspace = None;
                (
                    retired,
                    ServiceStateEvent::transition(generation, LifecycleStatus::Failed),
                    None,
                )
            }
        };
        drop(retired);
        self.inner.event_sink.emit(event);
        if let Some((replacement_generation, workspace)) = restart {
            let _ = Box::pin(self.launch_reserved(replacement_generation, workspace)).await;
        }
    }
}

async fn wait_for_termination_or_timeout(
    termination: &mut watch::Receiver<bool>,
    timer: &dyn ReadinessTimer,
) -> bool {
    if *termination.borrow() {
        return true;
    }
    let mut terminal = Box::pin(async move {
        loop {
            if termination.changed().await.is_err() {
                return false;
            }
            if *termination.borrow_and_update() {
                return true;
            }
        }
    });
    let mut timeout = timer.wait(PROCESS_EXIT_TIMEOUT);
    poll_fn(|context| {
        if timeout.as_mut().poll(context).is_ready() {
            return Poll::Ready(false);
        }
        if let Poll::Ready(terminated) = terminal.as_mut().poll(context) {
            return Poll::Ready(terminated);
        }
        Poll::Pending
    })
    .await
}

enum ReadinessWait {
    Event(Option<ProcessEvent>),
    TimedOut,
}

async fn wait_for_event_or_timeout(
    mut event: Pin<Box<dyn Future<Output = Option<ProcessEvent>> + Send + '_>>,
    timeout: &mut Pin<Box<dyn Future<Output = ()> + Send>>,
) -> ReadinessWait {
    poll_fn(|context| {
        if timeout.as_mut().poll(context).is_ready() {
            return Poll::Ready(ReadinessWait::TimedOut);
        }
        if let Poll::Ready(event) = event.as_mut().poll(context) {
            return Poll::Ready(ReadinessWait::Event(event));
        }
        Poll::Pending
    })
    .await
}

async fn stop_startup_process(
    child: &mut dyn SidecarChild,
    events: &mut dyn SidecarEventStream,
    timer: &dyn ReadinessTimer,
) -> bool {
    if child.kill().is_err() {
        return false;
    }
    let mut timeout = timer.wait(PROCESS_EXIT_TIMEOUT);
    loop {
        let mut event = events.next_event();
        let outcome = poll_fn(|context| {
            let ready_event = match event.as_mut().poll(context) {
                Poll::Ready(event) => Some(event),
                Poll::Pending => None,
            };
            if matches!(ready_event, Some(Some(ProcessEvent::Terminated))) {
                return Poll::Ready(ReadinessWait::Event(ready_event.flatten()));
            }
            if timeout.as_mut().poll(context).is_ready() {
                return Poll::Ready(ReadinessWait::TimedOut);
            }
            if let Some(event) = ready_event {
                return Poll::Ready(ReadinessWait::Event(event));
            }
            Poll::Pending
        })
        .await;
        match outcome {
            ReadinessWait::Event(Some(ProcessEvent::Terminated)) => return true,
            ReadinessWait::Event(Some(
                ProcessEvent::Stdout(_)
                | ProcessEvent::Stderr(_)
                | ProcessEvent::OutputError
                | ProcessEvent::Error,
            )) => {}
            ReadinessWait::Event(None) | ReadinessWait::TimedOut => return false,
        }
    }
}

fn validate_canonical_workspace(workspace: &Path) -> Result<(), LifecycleError> {
    if !workspace.is_absolute() || !workspace.is_dir() {
        return Err(LifecycleError::InvalidWorkspace);
    }
    let resolved =
        std::fs::canonicalize(workspace).map_err(|_| LifecycleError::InvalidWorkspace)?;
    if resolved != workspace {
        return Err(LifecycleError::InvalidWorkspace);
    }
    Ok(())
}

fn validate_workspace_encoding(workspace: &Path) -> Result<(), LifecycleError> {
    workspace
        .to_str()
        .map(|_| ())
        .ok_or(LifecycleError::InvalidWorkspace)
}

fn build_bootstrap(workspace: &Path, token: &str) -> Result<BootstrapInput, LifecycleError> {
    let workspace = workspace.to_str().ok_or(LifecycleError::InvalidWorkspace)?;
    let mut bytes = serde_json::to_vec(&BootstrapDocument {
        record_type: "bootstrap",
        secret: token,
        workspace,
    })
    .map_err(|_| LifecycleError::StartupFailed)?;
    bytes.push(b'\n');
    Ok(BootstrapInput { bytes })
}

fn generate_token() -> String {
    let mut bytes = [0_u8; 32];
    rand::rng().fill_bytes(&mut bytes);
    base64::engine::general_purpose::URL_SAFE_NO_PAD.encode(bytes)
}

#[cfg(test)]
mod tests {
    use std::{
        collections::VecDeque,
        future,
        path::PathBuf,
        pin::Pin,
        sync::{
            Arc, Mutex as StdMutex, Weak,
            atomic::{AtomicBool, AtomicUsize, Ordering},
        },
        time::{Duration, Instant},
    };

    use base64::Engine as _;

    use super::*;

    struct FakeChild {
        control: Arc<FakeProcessControl>,
    }

    impl Drop for FakeChild {
        fn drop(&mut self) {
            let observed = self
                .control
                .drop_probe
                .lock()
                .expect("drop probe lock")
                .as_ref()
                .and_then(Weak::upgrade)
                .map(|inner| usize::from(inner.state.try_lock().is_ok()) + 1)
                .unwrap_or(0);
            self.control
                .drop_observation
                .store(observed, Ordering::SeqCst);
        }
    }

    impl SidecarChild for FakeChild {
        fn kill(&mut self) -> Result<(), LifecycleError> {
            self.control.kills.fetch_add(1, Ordering::SeqCst);
            if self.control.kill_fails.load(Ordering::SeqCst) {
                return Err(LifecycleError::ProcessControlFailed);
            }
            if self.control.terminate_on_kill.load(Ordering::SeqCst) {
                let _ = self.control.sender.send(ProcessEvent::Terminated);
            }
            Ok(())
        }
    }

    struct FakeEventStream {
        receiver: tokio::sync::mpsc::UnboundedReceiver<ProcessEvent>,
    }

    impl SidecarEventStream for FakeEventStream {
        fn next_event(
            &mut self,
        ) -> Pin<Box<dyn Future<Output = Option<ProcessEvent>> + Send + '_>> {
            Box::pin(self.receiver.recv())
        }
    }

    struct FakeProcessControl {
        sender: tokio::sync::mpsc::UnboundedSender<ProcessEvent>,
        kills: AtomicUsize,
        terminate_on_kill: AtomicBool,
        kill_fails: AtomicBool,
        drop_probe: StdMutex<Option<Weak<LifecycleInner>>>,
        drop_observation: AtomicUsize,
    }

    #[derive(Clone)]
    struct FakeProcess {
        control: Arc<FakeProcessControl>,
    }

    impl FakeProcess {
        fn send(&self, event: ProcessEvent) {
            assert!(self.control.sender.send(event).is_ok());
        }

        fn kills(&self) -> usize {
            self.control.kills.load(Ordering::SeqCst)
        }

        fn observe_drop_lock(&self, lifecycle: &ServiceLifecycle) {
            *self.control.drop_probe.lock().expect("drop probe lock") =
                Some(Arc::downgrade(&lifecycle.inner));
        }

        fn dropped_outside_state_lock(&self) -> bool {
            self.control.drop_observation.load(Ordering::SeqCst) == 2
        }
    }

    struct FakeLaunch {
        child: FakeChild,
        events: FakeEventStream,
    }

    #[derive(Default)]
    struct FakeSpawner {
        launches: StdMutex<VecDeque<FakeLaunch>>,
        bootstraps: StdMutex<Vec<Vec<u8>>>,
    }

    impl FakeSpawner {
        fn queue(&self, terminate_on_kill: bool) -> FakeProcess {
            self.queue_with_control(terminate_on_kill, false)
        }

        fn queue_with_kill_failure(&self) -> FakeProcess {
            self.queue_with_control(false, true)
        }

        fn queue_with_control(&self, terminate_on_kill: bool, kill_fails: bool) -> FakeProcess {
            let (sender, receiver) = tokio::sync::mpsc::unbounded_channel();
            let control = Arc::new(FakeProcessControl {
                sender,
                kills: AtomicUsize::new(0),
                terminate_on_kill: AtomicBool::new(terminate_on_kill),
                kill_fails: AtomicBool::new(kill_fails),
                drop_probe: StdMutex::new(None),
                drop_observation: AtomicUsize::new(0),
            });
            self.launches
                .lock()
                .expect("fake launch lock")
                .push_back(FakeLaunch {
                    child: FakeChild {
                        control: Arc::clone(&control),
                    },
                    events: FakeEventStream { receiver },
                });
            FakeProcess { control }
        }

        fn bootstraps(&self) -> Vec<Vec<u8>> {
            self.bootstraps.lock().expect("fake bootstrap lock").clone()
        }
    }

    impl SidecarSpawner for FakeSpawner {
        fn spawn(&self, bootstrap: BootstrapInput) -> Result<SpawnedSidecar, LifecycleError> {
            self.bootstraps
                .lock()
                .expect("fake bootstrap lock")
                .push(bootstrap.into_bytes());
            let launch = self
                .launches
                .lock()
                .expect("fake launch lock")
                .pop_front()
                .ok_or(LifecycleError::StartupFailed)?;
            Ok(SpawnedSidecar::new(
                Box::new(launch.child),
                Box::new(launch.events),
            ))
        }
    }

    struct FakeTokenSource {
        tokens: StdMutex<VecDeque<String>>,
    }

    impl FakeTokenSource {
        fn new(tokens: impl IntoIterator<Item = String>) -> Self {
            Self {
                tokens: StdMutex::new(tokens.into_iter().collect()),
            }
        }
    }

    impl TokenSource for FakeTokenSource {
        fn generate(&self) -> String {
            self.tokens
                .lock()
                .expect("fake token lock")
                .pop_front()
                .expect("test must provide a token for every launch")
        }
    }

    struct FakeTimer {
        immediate_readiness: bool,
        immediate_cleanup: bool,
        waits: StdMutex<Vec<Duration>>,
    }

    impl FakeTimer {
        fn pending() -> Self {
            Self {
                immediate_readiness: false,
                immediate_cleanup: false,
                waits: StdMutex::new(Vec::new()),
            }
        }

        fn immediate() -> Self {
            Self {
                immediate_readiness: true,
                immediate_cleanup: true,
                waits: StdMutex::new(Vec::new()),
            }
        }

        fn cleanup_immediate() -> Self {
            Self {
                immediate_readiness: false,
                immediate_cleanup: true,
                waits: StdMutex::new(Vec::new()),
            }
        }

        fn waits(&self) -> Vec<Duration> {
            self.waits.lock().expect("fake timer lock").clone()
        }
    }

    impl ReadinessTimer for FakeTimer {
        fn wait(&self, duration: Duration) -> Pin<Box<dyn Future<Output = ()> + Send>> {
            self.waits.lock().expect("fake timer lock").push(duration);
            if (duration == READINESS_TIMEOUT && self.immediate_readiness)
                || (duration == PROCESS_EXIT_TIMEOUT && self.immediate_cleanup)
            {
                Box::pin(future::ready(()))
            } else {
                Box::pin(future::pending())
            }
        }
    }

    struct FakeEventSink {
        events: StdMutex<Vec<ServiceStateEvent>>,
        updates: watch::Sender<u64>,
    }

    impl Default for FakeEventSink {
        fn default() -> Self {
            let (updates, _) = watch::channel(0);
            Self {
                events: StdMutex::new(Vec::new()),
                updates,
            }
        }
    }

    impl FakeEventSink {
        fn events(&self) -> Vec<ServiceStateEvent> {
            self.events.lock().expect("fake event lock").clone()
        }
    }

    impl LifecycleEventSink for FakeEventSink {
        fn emit(&self, event: ServiceStateEvent) {
            self.events.lock().expect("fake event lock").push(event);
            self.updates.send_modify(|revision| {
                *revision = revision.saturating_add(1);
            });
        }
    }

    fn canonical_workspace() -> PathBuf {
        std::fs::canonicalize(env!("CARGO_MANIFEST_DIR"))
            .expect("manifest directory must have a canonical path")
    }

    fn ready(port: u16) -> ProcessEvent {
        ProcessEvent::Stdout(
            format!("{{\"type\":\"server-ready\",\"host\":\"127.0.0.1\",\"port\":{port}}}\n")
                .into_bytes(),
        )
    }

    fn lifecycle(
        spawner: Arc<FakeSpawner>,
        tokens: impl IntoIterator<Item = String>,
        timer: Arc<FakeTimer>,
        events: Arc<FakeEventSink>,
    ) -> ServiceLifecycle {
        ServiceLifecycle::new(
            spawner,
            Arc::new(FakeTokenSource::new(tokens)),
            timer,
            events,
            canonical_workspace().join("sidecar-diagnostic.log"),
        )
    }

    async fn wait_for_status(
        events: &FakeEventSink,
        lifecycle: &ServiceLifecycle,
        expected: LifecycleStatus,
    ) {
        let mut updates = events.updates.subscribe();
        let reached = tokio::time::timeout(Duration::from_secs(5), async {
            loop {
                if lifecycle.status().await == expected {
                    return true;
                }
                if updates.changed().await.is_err() {
                    return false;
                }
            }
        })
        .await;
        if !matches!(reached, Ok(true)) {
            let observed = lifecycle.status().await;
            panic!("timed out waiting for lifecycle status {expected:?}; observed {observed:?}");
        }
    }

    async fn wait_for_token(events: &FakeEventSink, lifecycle: &ServiceLifecycle, expected: &str) {
        let mut updates = events.updates.subscribe();
        let reached = tokio::time::timeout(Duration::from_secs(5), async {
            loop {
                if lifecycle
                    .connection()
                    .await
                    .is_some_and(|connection| connection.token() == expected)
                {
                    return true;
                }
                if updates.changed().await.is_err() {
                    return false;
                }
            }
        })
        .await;
        if !matches!(reached, Ok(true)) {
            let observed_status = lifecycle.status().await;
            let connection_present = lifecycle.connection().await.is_some();
            panic!(
                "timed out waiting for replacement connection; observed status {observed_status:?}, connection present: {connection_present}"
            );
        }
    }

    #[test]
    fn generated_token_is_url_safe_unpadded_encoding_of_exactly_32_random_bytes() {
        let token = generate_token();

        assert!(token.len() == 43);
        assert!(
            token
                .bytes()
                .all(|byte| byte.is_ascii_alphanumeric() || byte == b'_' || byte == b'-')
        );
        let decoded = base64::engine::general_purpose::URL_SAFE_NO_PAD
            .decode(token.as_bytes())
            .expect("generated token must be URL-safe base64");
        assert!(decoded.len() == 32);
    }

    #[test]
    fn real_tauri_spawner_exposes_only_the_fixed_sidecar_constructor() {
        let _constructor: fn(tauri::AppHandle<tauri::Wry>) -> TauriSidecarSpawner<tauri::Wry> =
            TauriSidecarSpawner::new;

        assert!(TauriSidecarSpawner::<tauri::Wry>::program_name() == "agent-harness-sidecar");
    }

    #[test]
    fn real_sidecar_command_transport_rejects_any_explicit_argument_or_environment() {
        let clean = std::process::Command::new("fixed-sidecar");
        assert!(validate_sidecar_command_transport(&clean).is_ok());

        let mut with_argument = std::process::Command::new("fixed-sidecar");
        with_argument.arg("forbidden-capability");
        assert!(matches!(
            validate_sidecar_command_transport(&with_argument),
            Err(LifecycleError::StartupFailed)
        ));

        let mut with_environment = std::process::Command::new("fixed-sidecar");
        with_environment.env("FORBIDDEN_CAPABILITY", "present");
        assert!(matches!(
            validate_sidecar_command_transport(&with_environment),
            Err(LifecycleError::StartupFailed)
        ));
    }

    #[cfg(unix)]
    #[test]
    fn process_pipe_observes_one_line_eof_then_owned_child_is_killed_and_reaped() {
        tauri::async_runtime::block_on(async {
            let mut child = std::process::Command::new("/bin/sh")
                .args([
                    "-c",
                    "IFS= read -r first; if IFS= read -r extra; then exit 9; fi; printf eof-observed; exec /bin/sleep 30",
                ])
                .stdin(std::process::Stdio::piped())
                .stdout(std::process::Stdio::piped())
                .stderr(std::process::Stdio::null())
                .spawn()
                .expect("benign ownership-test child must spawn");
            let stdin = child.stdin.take().expect("test child stdin must be piped");
            let input = BootstrapInput {
                bytes: b"one bootstrap line\n".to_vec(),
            };
            write_bootstrap_and_close(stdin, &input).expect("bootstrap write must succeed");
            let mut stdout = child
                .stdout
                .take()
                .expect("test child stdout must be piped");
            let mut marker = [0_u8; 12];
            stdout
                .read_exact(&mut marker)
                .expect("child must report observing stdin EOF");
            assert!(marker == *b"eof-observed");

            let child = Arc::new(StdMutex::new(child));
            let (sender, mut receiver) = tokio::sync::mpsc::unbounded_channel();
            spawn_process_waiter(Arc::clone(&child), sender);
            let mut owned = TauriSidecarChild { child };

            owned.kill().expect("first kill must succeed");
            assert!(matches!(
                receiver.recv().await,
                Some(ProcessEvent::Terminated)
            ));
            owned
                .kill()
                .expect("kill after observed terminal state must be harmless");
        });
    }

    #[test]
    fn successful_start_writes_exact_bootstrap_and_stores_exact_connection() {
        tauri::async_runtime::block_on(async {
            let token = "A".repeat(43);
            let spawner = Arc::new(FakeSpawner::default());
            let process = spawner.queue(true);
            process.send(ProcessEvent::Stdout(
                br#"{"type":"server-ready","host":"127."#.to_vec(),
            ));
            process.send(ProcessEvent::Stdout(
                b"0.0.1\",\"port\":49152}\nignored".to_vec(),
            ));
            let lifecycle = lifecycle(
                Arc::clone(&spawner),
                [token.clone()],
                Arc::new(FakeTimer::pending()),
                Arc::new(FakeEventSink::default()),
            );
            let workspace = canonical_workspace();

            lifecycle
                .start(workspace.clone())
                .await
                .expect("ready sidecar must start");

            let connection = lifecycle
                .connection()
                .await
                .expect("ready start must store a connection");
            assert!(connection.base_url() == "http://127.0.0.1:49152");
            assert!(connection.token() == token);

            let bootstraps = spawner.bootstraps();
            assert!(bootstraps.len() == 1);
            let bootstrap = &bootstraps[0];
            assert!(bootstrap.ends_with(b"\n"));
            assert!(bootstrap.iter().filter(|byte| **byte == b'\n').count() == 1);
            let document: serde_json::Value =
                serde_json::from_slice(&bootstrap[..bootstrap.len() - 1])
                    .expect("bootstrap must be one JSON object");
            let object = document.as_object().expect("bootstrap must be an object");
            assert!(object.len() == 3);
            assert!(object.get("type").and_then(|value| value.as_str()) == Some("bootstrap"));
            assert!(object.get("secret").and_then(|value| value.as_str()) == Some(token.as_str()));
            assert!(object.get("workspace").and_then(|value| value.as_str()) == workspace.to_str());

            lifecycle
                .shutdown()
                .await
                .expect("test cleanup must stop child");
            assert!(process.kills() == 1);
        });
    }

    #[test]
    fn start_rejects_a_noncanonical_workspace_before_spawning() {
        tauri::async_runtime::block_on(async {
            let spawner = Arc::new(FakeSpawner::default());
            let lifecycle = lifecycle(
                Arc::clone(&spawner),
                ["O".repeat(43)],
                Arc::new(FakeTimer::pending()),
                Arc::new(FakeEventSink::default()),
            );
            let noncanonical = PathBuf::from(".");

            let result = lifecycle.start(noncanonical).await;

            assert!(matches!(result, Err(LifecycleError::InvalidWorkspace)));
            assert!(spawner.bootstraps().is_empty());
            assert!(lifecycle.status().await == LifecycleStatus::Stopped);
        });
    }

    #[cfg(unix)]
    #[test]
    fn start_rejects_a_non_utf8_workspace_before_reserving_lifecycle_state() {
        use std::{ffi::OsString, os::unix::ffi::OsStringExt as _};

        tauri::async_runtime::block_on(async {
            let mut workspace_bytes = b"/tmp/agent-harness-non-utf8-".to_vec();
            workspace_bytes.push(0xff);
            let workspace = PathBuf::from(OsString::from_vec(workspace_bytes));
            assert!(workspace.to_str().is_none());
            assert!(matches!(
                validate_workspace_encoding(&workspace),
                Err(LifecycleError::InvalidWorkspace)
            ));

            let spawner = Arc::new(FakeSpawner::default());
            let lifecycle = lifecycle(
                Arc::clone(&spawner),
                ["P".repeat(43)],
                Arc::new(FakeTimer::pending()),
                Arc::new(FakeEventSink::default()),
            );

            let result = lifecycle.start(workspace).await;
            let status = lifecycle.status().await;
            let bootstraps = spawner.bootstraps();

            assert!(matches!(result, Err(LifecycleError::InvalidWorkspace)));
            assert!(status == LifecycleStatus::Stopped);
            assert!(bootstraps.is_empty());
        });
    }

    #[test]
    fn readiness_timeout_uses_fifteen_second_contract_without_wall_clock_sleep() {
        tauri::async_runtime::block_on(async {
            let spawner = Arc::new(FakeSpawner::default());
            let process = spawner.queue(true);
            let timer = Arc::new(FakeTimer::immediate());
            let lifecycle = lifecycle(
                Arc::clone(&spawner),
                ["B".repeat(43)],
                Arc::clone(&timer),
                Arc::new(FakeEventSink::default()),
            );
            let started = Instant::now();

            let result = lifecycle.start(canonical_workspace()).await;

            assert!(matches!(result, Err(LifecycleError::ReadinessTimedOut)));
            assert!(started.elapsed() < Duration::from_secs(1));
            assert!(timer.waits() == [READINESS_TIMEOUT, PROCESS_EXIT_TIMEOUT]);
            assert!(process.kills() == 1);
        });
    }

    #[test]
    fn readiness_deadline_wins_when_output_is_already_saturated() {
        tauri::async_runtime::block_on(async {
            let event: Pin<Box<dyn Future<Output = Option<ProcessEvent>> + Send>> =
                Box::pin(future::ready(Some(ProcessEvent::Stderr(vec![b'x']))));
            let mut timeout: Pin<Box<dyn Future<Output = ()> + Send>> = Box::pin(future::ready(()));

            assert!(matches!(
                wait_for_event_or_timeout(event, &mut timeout).await,
                ReadinessWait::TimedOut
            ));
        });
    }

    #[test]
    fn termination_or_error_before_readiness_fails_without_disclosing_process_detail() {
        tauri::async_runtime::block_on(async {
            let terminated_spawner = Arc::new(FakeSpawner::default());
            let terminated = terminated_spawner.queue(false);
            terminated.send(ProcessEvent::Terminated);
            let terminated_lifecycle = lifecycle(
                terminated_spawner,
                ["C".repeat(43)],
                Arc::new(FakeTimer::pending()),
                Arc::new(FakeEventSink::default()),
            );

            let terminated_result = terminated_lifecycle.start(canonical_workspace()).await;
            assert!(matches!(
                terminated_result,
                Err(LifecycleError::StartupTerminated)
            ));
            assert!(terminated.kills() == 0);

            let error_spawner = Arc::new(FakeSpawner::default());
            let errored = error_spawner.queue(true);
            errored.send(ProcessEvent::Error);
            let error_lifecycle = lifecycle(
                error_spawner,
                ["D".repeat(43)],
                Arc::new(FakeTimer::pending()),
                Arc::new(FakeEventSink::default()),
            );

            let error_result = error_lifecycle.start(canonical_workspace()).await;
            assert!(matches!(error_result, Err(LifecycleError::StartupFailed)));
            assert!(errored.kills() == 1);
        });
    }

    #[test]
    fn startup_cleanup_is_bounded_and_retains_unterminated_child_authority() {
        tauri::async_runtime::block_on(async {
            let kill_failure_spawner = Arc::new(FakeSpawner::default());
            let kill_failure = kill_failure_spawner.queue_with_kill_failure();
            kill_failure.send(ProcessEvent::Stdout(b"not-json\n".to_vec()));
            let kill_failure_lifecycle = lifecycle(
                kill_failure_spawner,
                ["X".repeat(43)],
                Arc::new(FakeTimer::cleanup_immediate()),
                Arc::new(FakeEventSink::default()),
            );

            let result = tokio::time::timeout(
                Duration::from_millis(100),
                kill_failure_lifecycle.start(canonical_workspace()),
            )
            .await
            .expect("startup kill failure must not wait forever");

            assert!(matches!(result, Err(LifecycleError::ReadinessFailed)));
            assert!(kill_failure_lifecycle.status().await == LifecycleStatus::Failed);
            assert!(kill_failure_lifecycle.has_active_process().await);
            assert!(matches!(
                kill_failure_lifecycle.restart_once().await,
                Err(LifecycleError::RestartRefused)
            ));
            kill_failure.send(ProcessEvent::Terminated);
            tokio::time::timeout(Duration::from_secs(1), async {
                while kill_failure_lifecycle.has_active_process().await {
                    tokio::time::sleep(Duration::from_millis(1)).await;
                }
            })
            .await
            .expect("terminal authority must release retained startup child");
            assert!(!kill_failure_lifecycle.has_active_process().await);

            let missing_terminal_spawner = Arc::new(FakeSpawner::default());
            let missing_terminal = missing_terminal_spawner.queue(false);
            missing_terminal.send(ProcessEvent::Stdout(b"not-json\n".to_vec()));
            let missing_terminal_lifecycle = lifecycle(
                missing_terminal_spawner,
                ["Y".repeat(43)],
                Arc::new(FakeTimer::cleanup_immediate()),
                Arc::new(FakeEventSink::default()),
            );

            let result = tokio::time::timeout(
                Duration::from_millis(100),
                missing_terminal_lifecycle.start(canonical_workspace()),
            )
            .await
            .expect("missing startup terminal event must be deadline bounded");

            assert!(matches!(result, Err(LifecycleError::ReadinessFailed)));
            assert!(missing_terminal_lifecycle.status().await == LifecycleStatus::Failed);
            assert!(missing_terminal_lifecycle.has_active_process().await);
            assert!(missing_terminal.kills() == 1);
        });
    }

    #[test]
    fn clean_shutdown_marks_intent_kills_and_awaits_once_without_restart() {
        tauri::async_runtime::block_on(async {
            let spawner = Arc::new(FakeSpawner::default());
            let process = spawner.queue(true);
            process.send(ready(49_152));
            let events = Arc::new(FakeEventSink::default());
            let lifecycle = lifecycle(
                Arc::clone(&spawner),
                ["E".repeat(43)],
                Arc::new(FakeTimer::pending()),
                Arc::clone(&events),
            );
            lifecycle
                .start(canonical_workspace())
                .await
                .expect("ready sidecar must start");

            lifecycle.shutdown().await.expect("shutdown must succeed");
            lifecycle
                .shutdown()
                .await
                .expect("duplicate stopped shutdown must be idempotent");

            assert!(lifecycle.status().await == LifecycleStatus::Stopped);
            assert!(lifecycle.connection().await.is_none());
            assert!(process.kills() == 1);
            assert!(spawner.bootstraps().len() == 1);
            assert!(
                !events
                    .events()
                    .iter()
                    .any(|event| event.status() == LifecycleStatus::Restarting)
            );
        });
    }

    #[test]
    fn late_same_generation_terminal_handler_is_idempotent_after_clean_shutdown() {
        tauri::async_runtime::block_on(async {
            let spawner = Arc::new(FakeSpawner::default());
            let process = spawner.queue(true);
            process.send(ready(49_152));
            let lifecycle = lifecycle(
                Arc::clone(&spawner),
                ["Z".repeat(43)],
                Arc::new(FakeTimer::pending()),
                Arc::new(FakeEventSink::default()),
            );
            lifecycle
                .start(canonical_workspace())
                .await
                .expect("ready sidecar must start");
            lifecycle.shutdown().await.expect("shutdown must succeed");

            lifecycle.handle_process_end(1).await;

            assert!(lifecycle.status().await == LifecycleStatus::Stopped);
            assert!(lifecycle.connection().await.is_none());
            assert!(spawner.bootstraps().len() == 1);
        });
    }

    #[test]
    fn shutdown_kill_failure_is_bounded_and_retains_child_until_terminal_authority() {
        tauri::async_runtime::block_on(async {
            let spawner = Arc::new(FakeSpawner::default());
            let process = spawner.queue_with_kill_failure();
            process.send(ready(49_152));
            let events = Arc::new(FakeEventSink::default());
            let lifecycle = lifecycle(
                spawner,
                ["Q".repeat(43)],
                Arc::new(FakeTimer::cleanup_immediate()),
                Arc::clone(&events),
            );
            lifecycle
                .start(canonical_workspace())
                .await
                .expect("ready sidecar must start");

            let result = tokio::time::timeout(Duration::from_millis(100), lifecycle.shutdown())
                .await
                .expect("kill failure cleanup must not wait forever");

            assert!(matches!(result, Err(LifecycleError::ProcessControlFailed)));
            assert!(lifecycle.status().await == LifecycleStatus::Failed);
            assert!(lifecycle.has_active_process().await);
            assert!(process.kills() == 1);
            assert!(matches!(
                lifecycle.restart_once().await,
                Err(LifecycleError::RestartRefused)
            ));

            process.send(ProcessEvent::Terminated);
            wait_for_status(&events, &lifecycle, LifecycleStatus::Stopped).await;
            assert!(!lifecycle.has_active_process().await);
        });
    }

    #[test]
    fn shutdown_missing_terminal_event_is_bounded_and_retains_child() {
        tauri::async_runtime::block_on(async {
            let spawner = Arc::new(FakeSpawner::default());
            let process = spawner.queue(false);
            process.send(ready(49_152));
            let events = Arc::new(FakeEventSink::default());
            let lifecycle = lifecycle(
                spawner,
                ["R".repeat(43)],
                Arc::new(FakeTimer::cleanup_immediate()),
                Arc::clone(&events),
            );
            lifecycle
                .start(canonical_workspace())
                .await
                .expect("ready sidecar must start");

            let result = tokio::time::timeout(Duration::from_millis(100), lifecycle.shutdown())
                .await
                .expect("missing terminal cleanup must be deadline bounded");

            assert!(matches!(result, Err(LifecycleError::ProcessControlFailed)));
            assert!(lifecycle.status().await == LifecycleStatus::Failed);
            assert!(lifecycle.has_active_process().await);
            assert!(process.kills() == 1);

            process.send(ProcessEvent::Terminated);
            wait_for_status(&events, &lifecycle, LifecycleStatus::Stopped).await;
        });
    }

    #[test]
    fn output_reader_error_does_not_masquerade_as_reaped_process_termination() {
        tauri::async_runtime::block_on(async {
            let spawner = Arc::new(FakeSpawner::default());
            let process = spawner.queue(true);
            process.send(ready(49_152));
            let lifecycle = lifecycle(
                spawner,
                ["S".repeat(43)],
                Arc::new(FakeTimer::pending()),
                Arc::new(FakeEventSink::default()),
            );
            lifecycle
                .start(canonical_workspace())
                .await
                .expect("ready sidecar must start");

            process.send(ProcessEvent::OutputError);
            tokio::task::yield_now().await;

            assert!(lifecycle.status().await == LifecycleStatus::Ready);
            assert!(lifecycle.connection().await.is_some());
            assert!(lifecycle.has_active_process().await);
            lifecycle
                .shutdown()
                .await
                .expect("terminal event after kill must complete shutdown");
        });
    }

    #[test]
    fn unexpected_exit_restarts_once_with_a_fresh_typed_connection() {
        tauri::async_runtime::block_on(async {
            let first_token = "F".repeat(43);
            let second_token = "G".repeat(43);
            let spawner = Arc::new(FakeSpawner::default());
            let first = spawner.queue(false);
            let second = spawner.queue(true);
            first.send(ready(49_152));
            second.send(ready(49_153));
            let events = Arc::new(FakeEventSink::default());
            let lifecycle = lifecycle(
                Arc::clone(&spawner),
                [first_token.clone(), second_token.clone()],
                Arc::new(FakeTimer::pending()),
                Arc::clone(&events),
            );
            lifecycle
                .start(canonical_workspace())
                .await
                .expect("first generation must start");

            first.send(ProcessEvent::Terminated);
            wait_for_token(&events, &lifecycle, &second_token).await;

            let connection = lifecycle
                .connection()
                .await
                .expect("replacement connection must be stored");
            assert!(connection.base_url() == "http://127.0.0.1:49153");
            assert!(connection.token() == second_token);
            assert!(connection.token() != first_token);
            assert!(lifecycle.restart_count().await == 1);
            assert!(spawner.bootstraps().len() == 2);
            let captured = events.events();
            assert!(
                captured
                    .iter()
                    .any(|event| event.status() == LifecycleStatus::Restarting)
            );
            assert!(captured.iter().any(|event| {
                event.status() == LifecycleStatus::Ready
                    && event
                        .connection()
                        .is_some_and(|connection| connection.token() == second_token)
            }));
            for event in &captured {
                let serialized = serde_json::to_string(event).expect("state event must serialize");
                if serialized.contains(&first_token) || serialized.contains(&second_token) {
                    assert!(event.status() == LifecycleStatus::Ready);
                    assert!(event.connection().is_some());
                }
            }

            lifecycle
                .shutdown()
                .await
                .expect("test cleanup must stop replacement");
            assert!(first.kills() == 0);
            assert!(second.kills() == 1);
        });
    }

    #[test]
    fn second_unexpected_exit_stays_failed_and_retains_only_log_authority() {
        tauri::async_runtime::block_on(async {
            let spawner = Arc::new(FakeSpawner::default());
            let first = spawner.queue(false);
            let second = spawner.queue(false);
            first.send(ready(49_152));
            second.send(ready(49_153));
            let events = Arc::new(FakeEventSink::default());
            let lifecycle = lifecycle(
                Arc::clone(&spawner),
                ["H".repeat(43), "I".repeat(43)],
                Arc::new(FakeTimer::pending()),
                Arc::clone(&events),
            );
            let expected_log = canonical_workspace().join("sidecar-diagnostic.log");
            lifecycle
                .start(canonical_workspace())
                .await
                .expect("first generation must start");
            first.send(ProcessEvent::Terminated);
            wait_for_token(&events, &lifecycle, &"I".repeat(43)).await;

            second.send(ProcessEvent::Terminated);
            wait_for_status(&events, &lifecycle, LifecycleStatus::Failed).await;

            assert!(lifecycle.connection().await.is_none());
            assert!(lifecycle.restart_count().await == 1);
            assert!(spawner.bootstraps().len() == 2);
            assert!(lifecycle.diagnostic_log_path().await == expected_log);
            assert!(!lifecycle.has_restart_workspace().await);
            assert!(matches!(
                lifecycle.restart_once().await,
                Err(LifecycleError::RestartRefused)
            ));
        });
    }

    #[test]
    fn failed_replacement_clears_exhausted_restart_workspace_authority() {
        tauri::async_runtime::block_on(async {
            let spawner = Arc::new(FakeSpawner::default());
            let first = spawner.queue(false);
            first.send(ready(49_152));
            let events = Arc::new(FakeEventSink::default());
            let lifecycle = lifecycle(
                spawner,
                ["T".repeat(43), "U".repeat(43)],
                Arc::new(FakeTimer::pending()),
                Arc::clone(&events),
            );
            lifecycle
                .start(canonical_workspace())
                .await
                .expect("first generation must start");

            first.send(ProcessEvent::Terminated);
            wait_for_status(&events, &lifecycle, LifecycleStatus::Failed).await;

            assert!(lifecycle.restart_count().await == 1);
            assert!(lifecycle.connection().await.is_none());
            assert!(!lifecycle.has_restart_workspace().await);
            assert!(matches!(
                lifecycle.restart_once().await,
                Err(LifecycleError::RestartRefused)
            ));
        });
    }

    #[test]
    fn retired_child_is_dropped_only_after_releasing_async_state_lock() {
        tauri::async_runtime::block_on(async {
            let spawner = Arc::new(FakeSpawner::default());
            let first = spawner.queue(false);
            first.send(ready(49_152));
            let events = Arc::new(FakeEventSink::default());
            let lifecycle = lifecycle(
                spawner,
                ["V".repeat(43), "W".repeat(43)],
                Arc::new(FakeTimer::pending()),
                Arc::clone(&events),
            );
            first.observe_drop_lock(&lifecycle);
            lifecycle
                .start(canonical_workspace())
                .await
                .expect("first generation must start");

            first.send(ProcessEvent::Terminated);
            tokio::time::timeout(Duration::from_secs(1), async {
                while first.control.drop_observation.load(Ordering::SeqCst) == 0 {
                    tokio::time::sleep(Duration::from_millis(1)).await;
                }
            })
            .await
            .expect("retired child must be dropped");

            assert!(first.dropped_outside_state_lock());
            wait_for_status(&events, &lifecycle, LifecycleStatus::Failed).await;
        });
    }

    #[test]
    fn stale_old_generation_event_cannot_overwrite_replacement() {
        tauri::async_runtime::block_on(async {
            let replacement_token = "K".repeat(43);
            let spawner = Arc::new(FakeSpawner::default());
            let first = spawner.queue(false);
            let second = spawner.queue(true);
            first.send(ready(49_152));
            second.send(ready(49_153));
            let events = Arc::new(FakeEventSink::default());
            let lifecycle = lifecycle(
                spawner,
                ["J".repeat(43), replacement_token.clone()],
                Arc::new(FakeTimer::pending()),
                Arc::clone(&events),
            );
            lifecycle
                .start(canonical_workspace())
                .await
                .expect("first generation must start");
            first.send(ProcessEvent::Terminated);
            wait_for_token(&events, &lifecycle, &replacement_token).await;

            lifecycle.handle_process_end(1).await;

            assert!(lifecycle.status().await == LifecycleStatus::Ready);
            assert!(
                lifecycle
                    .connection()
                    .await
                    .is_some_and(|connection| connection.token() == replacement_token)
            );
            lifecycle
                .shutdown()
                .await
                .expect("test cleanup must stop replacement");
        });
    }

    #[test]
    fn concurrent_duplicate_start_shutdown_and_restart_do_not_double_control_children() {
        tauri::async_runtime::block_on(async {
            let spawner = Arc::new(FakeSpawner::default());
            let first = spawner.queue(false);
            let replacement = spawner.queue(false);
            let lifecycle = lifecycle(
                Arc::clone(&spawner),
                ["L".repeat(43), "M".repeat(43)],
                Arc::new(FakeTimer::pending()),
                Arc::new(FakeEventSink::default()),
            );
            let workspace = canonical_workspace();
            let starting_lifecycle = lifecycle.clone();
            let starting_workspace = workspace.clone();
            let starting = tauri::async_runtime::spawn(async move {
                starting_lifecycle.start(starting_workspace).await
            });
            while spawner.bootstraps().is_empty() {
                tokio::task::yield_now().await;
            }
            assert!(matches!(
                lifecycle.start(workspace.clone()).await,
                Err(LifecycleError::Busy)
            ));
            first.send(ProcessEvent::Terminated);
            assert!(matches!(
                starting.await.expect("start task must join"),
                Err(LifecycleError::StartupTerminated)
            ));

            let restarting_lifecycle = lifecycle.clone();
            let restarting =
                tauri::async_runtime::spawn(
                    async move { restarting_lifecycle.restart_once().await },
                );
            while spawner.bootstraps().len() < 2 {
                tokio::task::yield_now().await;
            }
            assert!(matches!(
                lifecycle.restart_once().await,
                Err(LifecycleError::Busy)
            ));
            replacement.send(ready(49_154));
            restarting
                .await
                .expect("restart task must join")
                .expect("reserved restart must succeed");
            assert!(matches!(
                lifecycle.start(workspace).await,
                Err(LifecycleError::AlreadyStarted)
            ));

            let shutting_lifecycle = lifecycle.clone();
            let shutting =
                tauri::async_runtime::spawn(async move { shutting_lifecycle.shutdown().await });
            while replacement.kills() == 0 {
                tokio::task::yield_now().await;
            }
            assert!(matches!(
                lifecycle.shutdown().await,
                Err(LifecycleError::Busy)
            ));
            replacement.send(ProcessEvent::Terminated);
            shutting
                .await
                .expect("shutdown task must join")
                .expect("first shutdown must succeed");
            assert!(replacement.kills() == 1);
            assert!(spawner.bootstraps().len() == 2);
        });
    }

    #[test]
    fn secret_is_absent_from_errors_state_events_and_diagnostic_authority() {
        tauri::async_runtime::block_on(async {
            let secret = "N".repeat(43);
            let spawner = Arc::new(FakeSpawner::default());
            let process = spawner.queue(true);
            process.send(ProcessEvent::Stdout(b"not-json\n".to_vec()));
            let events = Arc::new(FakeEventSink::default());
            let lifecycle = lifecycle(
                spawner,
                [secret.clone()],
                Arc::new(FakeTimer::pending()),
                Arc::clone(&events),
            );

            let error = match lifecycle.start(canonical_workspace()).await {
                Err(error) => error,
                Ok(_) => panic!("malformed readiness unexpectedly succeeded"),
            };

            assert!(!error.to_string().contains(&secret));
            assert!(!format!("{error:?}").contains(&secret));
            assert!(
                !lifecycle
                    .diagnostic_log_path()
                    .await
                    .to_string_lossy()
                    .contains(&secret)
            );
            for event in events.events() {
                let serialized = serde_json::to_string(&event).expect("state event must serialize");
                assert!(!serialized.contains(&secret));
            }
        });
    }
}
