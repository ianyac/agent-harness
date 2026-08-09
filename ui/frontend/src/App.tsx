import { useEffect, useMemo, useRef, useState } from "react";

import { ApiClient } from "./api/http";
import { CommandPalette } from "./components/CommandPalette";
import { ConnectionStatus as ServiceStatus } from "./components/ConnectionStatus";
import { RecoveryView } from "./components/RecoveryView";
import { Composer } from "./features/conversation/Composer";
import type { DraftStorage } from "./features/conversation/useDraft";
import { Conversation } from "./features/conversation/Conversation";
import { Onboarding } from "./features/onboarding/Onboarding";
import { ActivityInspector } from "./features/inspector/ActivityInspector";
import { useInspector } from "./features/inspector/useInspector";
import type { InspectorStorage } from "./features/inspector/useInspector";
import type { CopyText } from "./features/conversation/CodeBlock";
import { ConversationHeader } from "./features/sessions/ConversationHeader";
import { SessionSidebar } from "./features/sessions/SessionSidebar";
import { SessionOperationRecovery } from "./features/sessions/SessionOperationRecovery";
import type { ConnectionStatus } from "./features/sessions/SessionSidebar";
import type { SessionRuntimeState } from "./features/sessions/useSessions";
import { useSessions } from "./features/sessions/useSessions";
import { NotificationObserver } from "./features/settings/NotificationObserver";
import { Settings } from "./features/settings/Settings";
import type { PreferenceStorage } from "./features/settings/preferences";
import { usePreferences } from "./features/settings/preferences";
import type { PlatformAdapter } from "./platform/types";
import { emptyTranscript } from "./protocol/reducer";
import type { ClientEvent, SendMessage, TranscriptState, TranscriptTerminal } from "./protocol/types";

const emptyTranscriptState = emptyTranscript();

type RetainedSubmission = {
  readonly client: ApiClient;
  readonly dispatcher: AppProps["onSessionEvent"];
  readonly generation: number;
  readonly event: SendMessage;
};

type RetryAuthority = {
  readonly key: string;
  readonly sessionId: string;
  readonly client: ApiClient;
  readonly dispatcher: AppProps["onSessionEvent"];
  readonly terminal: TranscriptTerminal;
  readonly event: SendMessage;
};

type BootstrapLifecycle = {
  readonly status: "checking" | "reconnecting";
  readonly attempt: number;
  readonly retrying: boolean;
};

function terminalKey(sessionId: string, terminal: TranscriptTerminal): string {
  return `${sessionId}:${terminal.generation}:${terminal.sequence}:${terminal.turnId}`;
}

type AppProps = {
  readonly client?: ApiClient;
  readonly runtimeBySession?: Readonly<Record<string, SessionRuntimeState>>;
  readonly transcriptBySession?: Readonly<Record<string, TranscriptState>>;
  readonly branchBySession?: Readonly<Record<string, string | null>>;
  readonly onSessionEvent?: (sessionId: string, event: ClientEvent) => void | Promise<void>;
  readonly onStopSession?: (sessionId: string) => void | Promise<void>;
  readonly onOpenSettings?: () => void;
  readonly onToggleActivity?: () => void;
  readonly onLocateWorkspace?: (sessionId: string, workspace: string) => void | Promise<void>;
  readonly platformAdapter?: PlatformAdapter;
  readonly preferenceStorage?: PreferenceStorage;
  readonly sidebarStorage?: Pick<Storage, "getItem" | "setItem">;
  readonly draftStorage?: DraftStorage;
  readonly inspectorStorage?: InspectorStorage;
  readonly inspectorCopyText?: CopyText;
};

export function App({
  client: providedClient,
  runtimeBySession = {},
  transcriptBySession = {},
  branchBySession = {},
  onSessionEvent = () => {},
  onStopSession = () => {},
  onOpenSettings = () => {},
  onToggleActivity = () => {},
  onLocateWorkspace = () => {},
  platformAdapter,
  preferenceStorage,
  sidebarStorage,
  draftStorage,
  inspectorStorage,
  inspectorCopyText,
}: AppProps) {
  const [connection, setConnection] = useState<{
    readonly client: ApiClient | null;
    readonly status: ConnectionStatus;
    readonly platform: PlatformAdapter | null;
  }>(() =>
    providedClient === undefined
      ? { client: null, status: "connecting", platform: platformAdapter ?? null }
      : { client: providedClient, status: "connected", platform: platformAdapter ?? null },
  );
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [bootstrapAttempt, setBootstrapAttempt] = useState(0);
  const [bootstrapLifecycle, setBootstrapLifecycle] = useState<BootstrapLifecycle | null>(() =>
    providedClient === undefined
      ? { status: "checking", attempt: 0, retrying: true }
      : null,
  );
  const [, setRetryRevision] = useState(0);
  const composerDraftMemory = useRef(new Map<string, string>());
  const retainedSubmissionRef = useRef(new Map<string, RetainedSubmission>());
  const retrySubmittedRef = useRef(new Set<string>());
  const bootstrapPendingRef = useRef(providedClient === undefined);
  const preferenceModel = usePreferences(preferenceStorage ?? sidebarStorage);

  useEffect(() => {
    if (providedClient !== undefined) {
      bootstrapPendingRef.current = false;
      setBootstrapLifecycle(null);
      setConnection({ client: providedClient, status: "connected", platform: platformAdapter ?? null });
      return;
    }
    let current = true;
    let currentPlatform = platformAdapter ?? null;
    bootstrapPendingRef.current = true;
    setBootstrapLifecycle({ status: "checking", attempt: bootstrapAttempt, retrying: true });
    setConnection({ client: null, status: "connecting", platform: platformAdapter ?? null });
    const loadPlatform = platformAdapter === undefined
      ? import("./platform").then(({ platform }) => platform)
      : Promise.resolve(platformAdapter);
    void loadPlatform
      .then(async (platform) => {
        currentPlatform = platform;
        const service = await platform.getServiceConnection();
        const client = new ApiClient(service);
        await client.get<{ readonly status: string }>("/api/health");
        return { platform, client };
      })
      .then(({ platform, client }) => {
        if (current) {
          bootstrapPendingRef.current = false;
          setBootstrapLifecycle(null);
          setConnection({ client, status: "connected", platform });
        }
      })
      .catch(() => {
        if (current) {
          bootstrapPendingRef.current = false;
          setBootstrapLifecycle({
            status: "reconnecting",
            attempt: bootstrapAttempt,
            retrying: false,
          });
          setConnection({ client: null, status: "connecting", platform: currentPlatform });
        }
      });
    return () => {
      current = false;
    };
  }, [bootstrapAttempt, platformAdapter, providedClient]);

  const retryBootstrap = () => {
    if (providedClient !== undefined || bootstrapPendingRef.current) return;
    bootstrapPendingRef.current = true;
    setBootstrapAttempt((attempt) => attempt + 1);
  };

  const sessionsModel = useSessions(connection.client);
  const activeSession = useMemo(
    () =>
      sessionsModel.sessions.find(
        (session) => session.session_id === sessionsModel.activeSessionId,
      ) ?? null,
    [sessionsModel.activeSessionId, sessionsModel.sessions],
  );
  const activeTranscript = activeSession === null
    ? null
    : transcriptBySession[activeSession.session_id] ?? emptyTranscriptState;
  const latestRetryContextRef = useRef({
    client: connection.client,
    activeSessionId: activeSession?.session_id ?? null,
    transcriptBySession,
    dispatcher: onSessionEvent,
  });
  latestRetryContextRef.current = {
    client: connection.client,
    activeSessionId: activeSession?.session_id ?? null,
    transcriptBySession,
    dispatcher: onSessionEvent,
  };
  const routeSessionEvent = (sessionId: string, clientEvent: ClientEvent) => {
    if (clientEvent.type === "send_message" && connection.client !== null) {
      const transcript = transcriptBySession[sessionId] ?? emptyTranscriptState;
      retainedSubmissionRef.current.set(sessionId, {
        client: connection.client,
        dispatcher: onSessionEvent,
        generation: transcript.generation,
        event: { ...clientEvent },
      });
    }
    return onSessionEvent(sessionId, clientEvent);
  };
  const retryAuthority = (() => {
    if (
      connection.client === null
      || activeSession === null
      || activeTranscript === null
      || activeTranscript.error === null
      || activeTranscript.terminal?.kind !== "failed"
    ) return null;
    const retained = retainedSubmissionRef.current.get(activeSession.session_id);
    if (
      retained === undefined
      || retained.client !== connection.client
      || retained.dispatcher !== onSessionEvent
      || retained.generation !== activeTranscript.terminal.generation
    ) return null;
    const key = terminalKey(activeSession.session_id, activeTranscript.terminal);
    if (retrySubmittedRef.current.has(key)) return null;
    return {
      key,
      sessionId: activeSession.session_id,
      client: connection.client,
      dispatcher: onSessionEvent,
      terminal: activeTranscript.terminal,
      event: retained.event,
    } satisfies RetryAuthority;
  })();
  const retryFailedSubmission = retryAuthority === null ? undefined : async () => {
    const authority = retryAuthority;
    const latest = latestRetryContextRef.current;
    const transcript = latest.transcriptBySession[authority.sessionId];
    const currentTerminal = transcript?.terminal;
    const retained = retainedSubmissionRef.current.get(authority.sessionId);
    if (
      latest.client !== authority.client
      || latest.dispatcher !== authority.dispatcher
      || latest.activeSessionId !== authority.sessionId
      || currentTerminal?.kind !== "failed"
      || terminalKey(authority.sessionId, currentTerminal) !== authority.key
      || retained?.client !== authority.client
      || retained.dispatcher !== authority.dispatcher
      || retained.generation !== authority.terminal.generation
      || retrySubmittedRef.current.has(authority.key)
    ) return;
    retrySubmittedRef.current.add(authority.key);
    try {
      await authority.dispatcher?.(authority.sessionId, authority.event);
      setRetryRevision((revision) => revision + 1);
    } catch (error) {
      retrySubmittedRef.current.delete(authority.key);
      setRetryRevision((revision) => revision + 1);
      throw error;
    }
  };
  const inspector = useInspector({
    sessionId: activeSession?.session_id ?? null,
    storage: inspectorStorage ?? sidebarStorage,
  });
  const activeElement = () => document.activeElement instanceof HTMLElement
    ? document.activeElement
    : null;
  const openInspectorOverview = () => {
    onToggleActivity();
    inspector.openOverview(activeElement());
  };
  const toggleInspector = () => {
    onToggleActivity();
    inspector.toggle(activeElement());
  };
  const openSettings = () => {
    onOpenSettings();
    setSettingsOpen(true);
  };
  const createFutureSession = async () => {
    const config = sessionsModel.config;
    if (config === null) return;
    await sessionsModel.createSession({
      workspace: config.base_workspace,
      mode: preferenceModel.preferences.defaultMode,
      contextMode: preferenceModel.preferences.contextMode,
      title: "New chat",
    });
  };
  const selectedPlatform = connection.platform ?? platformAdapter;
  const sessionReadiness = sessionsModel.refreshError !== null
    ? "reconnecting" as const
    : sessionsModel.loading ? "checking" as const : "connected" as const;
  const showingFirstRun = connection.client !== null
    && sessionsModel.sessions.length === 0
    && (sessionsModel.loading || sessionsModel.config !== null);

  if (showingFirstRun && selectedPlatform !== undefined && selectedPlatform !== null) {
    return (
      <>
        <Onboarding
          serviceStatus={sessionsModel.loading ? "checking" : "ready"}
          platform={selectedPlatform}
          defaultWorkspace={sessionsModel.config?.base_workspace ?? ""}
          defaultMode={preferenceModel.preferences.defaultMode}
          defaultContextMode={preferenceModel.preferences.contextMode}
          onCreate={sessionsModel.createSession}
          onInvalidateCreate={sessionsModel.invalidateCreate}
          authorityKey={connection.client}
        />
        <NotificationObserver
          clientKey={connection.client}
          platform={selectedPlatform}
          sessions={sessionsModel.sessions}
          activeSessionId={sessionsModel.activeSessionId}
          transcriptBySession={transcriptBySession}
          preferences={preferenceModel.preferences}
        />
      </>
    );
  }

  return (
    <div className="app-shell">
      <SessionSidebar
        sessions={sessionsModel.sessions}
        activeSessionId={sessionsModel.activeSessionId}
        runtimeBySession={runtimeBySession}
        connectionStatus={connection.client !== null && sessionReadiness !== "connected"
          ? "connecting"
          : connection.status}
        storage={sidebarStorage}
        collapsed={preferenceModel.preferences.sidebarCollapsed}
        onCollapsedChange={(sidebarCollapsed) => preferenceModel.update({ sidebarCollapsed })}
        onCreate={createFutureSession}
        onSelect={sessionsModel.selectSession}
        onRename={sessionsModel.renameSession}
        onArchive={sessionsModel.archiveSession}
        onSearch={() => setPaletteOpen(true)}
        onOpenSettings={openSettings}
      />
      <div className="conversation-shell">
        {connection.client === null && bootstrapLifecycle !== null ? (
          <div className="connection-lifecycle">
            <ServiceStatus
              status={bootstrapLifecycle.status}
              attemptKey={`bootstrap:${bootstrapLifecycle.attempt}`}
              retrying={bootstrapLifecycle.retrying}
              onRetry={retryBootstrap}
            />
          </div>
        ) : connection.client === null || sessionReadiness === "connected" ? null : (
          <div className="connection-lifecycle">
            <ServiceStatus
              status={sessionReadiness}
              attemptKey={sessionsModel.refreshError ?? connection.client}
              retrying={sessionsModel.loading}
              onRetry={() => void sessionsModel.refresh()}
            />
          </div>
        )}
        {activeSession === null ? (
          <header className="conversation-header">
            <h1>Agent Harness</h1>
          </header>
        ) : (
          <ConversationHeader
            key={activeSession.session_id}
            sessionId={activeSession.session_id}
            workspace={activeSession.workspace}
            branch={branchBySession[activeSession.session_id] ?? null}
            mode={activeSession.mode}
            onSetSessionMode={(event) => routeSessionEvent(activeSession.session_id, event)}
            onToggleActivity={openInspectorOverview}
          />
        )}
        <main aria-label="Conversation" className="conversation-main">
          {sessionsModel.operationError !== null ? (
            <SessionOperationRecovery
              failure={sessionsModel.operationError}
              onRetry={sessionsModel.retryOperation}
            />
          ) : activeTranscript?.error !== null && activeTranscript?.error !== undefined && activeSession !== null && selectedPlatform !== null && selectedPlatform !== undefined ? (
            <RecoveryView
              key={retryAuthority?.key ?? `${activeSession.session_id}:recovery`}
              error={{
                category: activeTranscript.error.category === "session_resume_error"
                  || activeTranscript.error.category === "session_resume_failure"
                  ? activeTranscript.error.category
                  : "turn_failure",
                message: activeTranscript.error.message,
              }}
              sessionId={activeSession.session_id}
              platform={selectedPlatform}
              onLocate={onLocateWorkspace}
              onArchive={sessionsModel.archiveSession}
              onRetry={retryFailedSubmission}
            />
          ) : activeTranscript === null ? null : (
            <Conversation
              key={`conversation-${activeSession?.session_id}`}
              state={activeTranscript}
              openInspector={(activityId) => {
                onToggleActivity();
                inspector.openActivity(activityId, activeElement());
              }}
              ownsSearchShortcut
              onSessionEvent={(event) => activeSession === null
                ? undefined
                : routeSessionEvent(activeSession.session_id, event)}
            />
          )}
        </main>
        {activeSession !== null && activeTranscript !== null ? (
          <Composer
            sessionId={activeSession.session_id}
            running={activeTranscript.running}
            stopping={activeTranscript.stopping}
            queued={activeTranscript.queued}
            onEvent={(event) => routeSessionEvent(activeSession.session_id, event)}
            onStop={() => onStopSession(activeSession.session_id)}
            draftStorage={draftStorage}
            draftMemory={composerDraftMemory.current}
          />
        ) : null}
      </div>
      {activeSession !== null && activeTranscript !== null ? (
        <ActivityInspector
          open={inspector.open}
          narrow={inspector.narrow}
          width={inspector.width}
          pinned={inspector.pinned}
          sessionId={activeSession.session_id}
          contextMode={activeSession.context_mode}
          state={activeTranscript}
          selectedActivityId={inspector.selectedActivityId}
          onClose={inspector.close}
          onPinnedChange={inspector.setPinned}
          onSelectActivity={inspector.selectActivity}
          onWidthChange={inspector.setWidth}
          copyText={inspectorCopyText}
        />
      ) : null}
      <CommandPalette
        open={paletteOpen}
        onOpenChange={setPaletteOpen}
        sessions={sessionsModel.sessions}
        onNewChat={createFutureSession}
        onOpenSettings={openSettings}
        onToggleActivity={toggleInspector}
        onSelectSession={sessionsModel.selectSession}
      />
      <Settings
        open={settingsOpen}
        onOpenChange={setSettingsOpen}
        preferences={preferenceModel.preferences}
        onChange={preferenceModel.update}
        workspace={activeSession?.workspace ?? sessionsModel.config?.base_workspace ?? null}
        logsSupported={selectedPlatform?.openLogs !== undefined}
      />
      {selectedPlatform === null || selectedPlatform === undefined ? null : (
        <NotificationObserver
          clientKey={connection.client}
          platform={selectedPlatform}
          sessions={sessionsModel.sessions}
          activeSessionId={sessionsModel.activeSessionId}
          transcriptBySession={transcriptBySession}
          preferences={preferenceModel.preferences}
        />
      )}
    </div>
  );
}
