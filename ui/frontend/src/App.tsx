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
import type { ConnectionStatus } from "./features/sessions/SessionSidebar";
import type { SessionRuntimeState } from "./features/sessions/useSessions";
import { useSessions } from "./features/sessions/useSessions";
import { NotificationObserver } from "./features/settings/NotificationObserver";
import { Settings } from "./features/settings/Settings";
import type { PreferenceStorage } from "./features/settings/preferences";
import { usePreferences } from "./features/settings/preferences";
import type { PlatformAdapter } from "./platform/types";
import { emptyTranscript } from "./protocol/reducer";
import type { ClientEvent, TranscriptState } from "./protocol/types";

const emptyTranscriptState = emptyTranscript();

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
  const composerDraftMemory = useRef(new Map<string, string>());
  const preferenceModel = usePreferences(preferenceStorage ?? sidebarStorage);

  useEffect(() => {
    if (providedClient !== undefined) {
      setConnection({ client: providedClient, status: "connected", platform: platformAdapter ?? null });
      return;
    }
    let current = true;
    setConnection({ client: null, status: "connecting", platform: platformAdapter ?? null });
    const loadPlatform = platformAdapter === undefined
      ? import("./platform").then(({ platform }) => platform)
      : Promise.resolve(platformAdapter);
    void loadPlatform
      .then(async (platform) => {
        const service = await platform.getServiceConnection();
        const client = new ApiClient(service);
        await client.get<{ readonly status: string }>("/api/health");
        return { platform, client };
      })
      .then(({ platform, client }) => {
        if (current) setConnection({ client, status: "connected", platform });
      })
      .catch(() => {
        if (current) setConnection({ client: null, status: "disconnected", platform: platformAdapter ?? null });
      });
    return () => {
      current = false;
    };
  }, [platformAdapter, providedClient]);

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
  const sessionReadiness = sessionsModel.error !== null
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
        {connection.client === null || sessionReadiness === "connected" ? null : (
          <div className="connection-lifecycle">
            <ServiceStatus
              status={sessionReadiness}
              attemptKey={sessionsModel.error ?? connection.client}
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
            onSetSessionMode={(event) => onSessionEvent(activeSession.session_id, event)}
            onToggleActivity={openInspectorOverview}
          />
        )}
        <main aria-label="Conversation" className="conversation-main">
          {activeTranscript?.error !== null && activeTranscript?.error !== undefined && activeSession !== null && selectedPlatform !== null && selectedPlatform !== undefined ? (
            <RecoveryView
              error={{ category: "turn_failure", message: activeTranscript.error.message }}
              sessionId={activeSession.session_id}
              platform={selectedPlatform}
              onLocate={onLocateWorkspace}
              onArchive={sessionsModel.archiveSession}
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
                : onSessionEvent(activeSession.session_id, event)}
            />
          )}
        </main>
        {activeSession !== null && activeTranscript !== null ? (
          <Composer
            sessionId={activeSession.session_id}
            running={activeTranscript.running}
            stopping={activeTranscript.stopping}
            queued={activeTranscript.queued}
            onEvent={(event) => onSessionEvent(activeSession.session_id, event)}
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
