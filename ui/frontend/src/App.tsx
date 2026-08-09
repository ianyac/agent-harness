import { useEffect, useMemo, useState } from "react";

import { ApiClient } from "./api/http";
import { CommandPalette } from "./components/CommandPalette";
import { ConversationHeader } from "./features/sessions/ConversationHeader";
import { SessionSidebar } from "./features/sessions/SessionSidebar";
import type { ConnectionStatus } from "./features/sessions/SessionSidebar";
import type { SessionRuntimeState } from "./features/sessions/useSessions";
import { useSessions } from "./features/sessions/useSessions";
import type { ClientEvent } from "./protocol/types";

type AppProps = {
  readonly client?: ApiClient;
  readonly runtimeBySession?: Readonly<Record<string, SessionRuntimeState>>;
  readonly branchBySession?: Readonly<Record<string, string | null>>;
  readonly onSessionEvent?: (sessionId: string, event: ClientEvent) => void;
  readonly onOpenSettings?: () => void;
  readonly onToggleActivity?: () => void;
  readonly sidebarStorage?: Pick<Storage, "getItem" | "setItem">;
};

export function App({
  client: providedClient,
  runtimeBySession = {},
  branchBySession = {},
  onSessionEvent = () => {},
  onOpenSettings = () => {},
  onToggleActivity = () => {},
  sidebarStorage,
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
            onToggleActivity={onToggleActivity}
          />
        )}
        <main aria-label="Conversation" className="transcript" />
      </div>
      <CommandPalette
        open={paletteOpen}
        onOpenChange={setPaletteOpen}
        sessions={sessionsModel.sessions}
        onNewChat={sessionsModel.createSession}
        onOpenSettings={onOpenSettings}
        onToggleActivity={onToggleActivity}
        onSelectSession={sessionsModel.selectSession}
      />
    </div>
  );
}
