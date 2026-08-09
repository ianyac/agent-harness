import { useEffect, useMemo, useRef, useState } from "react";

import { ApiClient } from "./api/http";
import { CommandPalette } from "./components/CommandPalette";
import { Composer } from "./features/conversation/Composer";
import type { DraftStorage } from "./features/conversation/useDraft";
import { Conversation } from "./features/conversation/Conversation";
import { ActivityInspector } from "./features/inspector/ActivityInspector";
import { useInspector } from "./features/inspector/useInspector";
import type { InspectorStorage } from "./features/inspector/useInspector";
import type { CopyText } from "./features/conversation/CodeBlock";
import { ConversationHeader } from "./features/sessions/ConversationHeader";
import { SessionSidebar } from "./features/sessions/SessionSidebar";
import type { ConnectionStatus } from "./features/sessions/SessionSidebar";
import type { SessionRuntimeState } from "./features/sessions/useSessions";
import { useSessions } from "./features/sessions/useSessions";
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
  sidebarStorage,
  draftStorage,
  inspectorStorage,
  inspectorCopyText,
}: AppProps) {
  const [connection, setConnection] = useState<{
    readonly client: ApiClient | null;
    readonly status: ConnectionStatus;
  }>(() =>
    providedClient === undefined
      ? { client: null, status: "connecting" }
      : { client: providedClient, status: "connected" },
  );
  const [paletteOpen, setPaletteOpen] = useState(false);
  const composerDraftMemory = useRef(new Map<string, string>());

  useEffect(() => {
    if (providedClient !== undefined) {
      setConnection({ client: providedClient, status: "connected" });
      return;
    }
    let current = true;
    setConnection({ client: null, status: "connecting" });
    void import("./platform")
      .then(({ platform }) => platform.getServiceConnection())
      .then((connection) => {
        if (current) setConnection({ client: new ApiClient(connection), status: "connected" });
      })
      .catch(() => {
        if (current) setConnection({ client: null, status: "disconnected" });
      });
    return () => {
      current = false;
    };
  }, [providedClient]);

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

  return (
    <div className="app-shell">
      <SessionSidebar
        sessions={sessionsModel.sessions}
        activeSessionId={sessionsModel.activeSessionId}
        runtimeBySession={runtimeBySession}
        connectionStatus={connection.status}
        storage={sidebarStorage}
        onCreate={sessionsModel.createSession}
        onSelect={sessionsModel.selectSession}
        onRename={sessionsModel.renameSession}
        onArchive={sessionsModel.archiveSession}
        onSearch={() => setPaletteOpen(true)}
        onOpenSettings={onOpenSettings}
      />
      <div className="conversation-shell">
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
          {activeTranscript === null ? null : (
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
        onNewChat={sessionsModel.createSession}
        onOpenSettings={onOpenSettings}
        onToggleActivity={toggleInspector}
        onSelectSession={sessionsModel.selectSession}
      />
    </div>
  );
}
