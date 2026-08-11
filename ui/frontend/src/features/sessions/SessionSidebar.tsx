import * as Tooltip from "@radix-ui/react-tooltip";

import { SessionRow } from "./SessionRow";
import type { SessionRecord, SessionRuntimeState } from "./useSessions";
import styles from "./sessionSidebar.module.css";

type GroupName = "Today" | "Yesterday" | "Earlier";

export type ConnectionStatus = "connecting" | "connected" | "disconnected";

type SessionSidebarProps = {
  readonly sessions: readonly SessionRecord[];
  readonly activeSessionId: string | null;
  readonly runtimeBySession: Readonly<Record<string, SessionRuntimeState>>;
  readonly connectionStatus?: ConnectionStatus;
  readonly now?: Date;
  readonly collapsed: boolean;
  readonly onCollapsedChange: (collapsed: boolean) => void;
  readonly onCreate: () => void | Promise<unknown>;
  readonly onSelect: (sessionId: string) => void;
  readonly onRename: (sessionId: string, title: string) => void | Promise<void>;
  readonly onArchive: (sessionId: string) => void | Promise<void>;
  readonly onSearch: () => void;
  readonly onOpenSettings: () => void;
  readonly settingsActive?: boolean;
};

function localDayOrdinal(value: Date): number {
  return Date.UTC(value.getFullYear(), value.getMonth(), value.getDate());
}

function groupName(session: SessionRecord, now: Date): GroupName {
  const timestamp = new Date(session.last_opened_at ?? session.updated_at);
  const days = Math.floor((localDayOrdinal(now) - localDayOrdinal(timestamp)) / 86_400_000);
  if (days <= 0) return "Today";
  if (days === 1) return "Yesterday";
  return "Earlier";
}

export function SessionSidebar({
  sessions,
  activeSessionId,
  runtimeBySession,
  connectionStatus = "connecting",
  now = new Date(),
  collapsed,
  onCollapsedChange,
  onCreate,
  onSelect,
  onRename,
  onArchive,
  onSearch,
  onOpenSettings,
  settingsActive = false,
}: SessionSidebarProps) {
  const groups: Record<GroupName, SessionRecord[]> = {
    Today: [],
    Yesterday: [],
    Earlier: [],
  };
  for (const session of sessions) groups[groupName(session, now)].push(session);

  const connectionLabel = {
    connecting: "Local service connecting",
    connected: "Local service connected",
    disconnected: "Local service disconnected",
  }[connectionStatus];
  const connectionCopy = {
    connecting: "Connecting",
    connected: "Connected",
    disconnected: "Disconnected",
  }[connectionStatus];

  return (
    <Tooltip.Provider delayDuration={0} skipDelayDuration={0}>
      <nav
        aria-label="Sessions"
        className={`session-sidebar ${styles.sidebar}`}
        data-collapsed={collapsed}
      >
      <div className={styles.identity}>
        <span className={styles.brandMark} aria-hidden="true" />
        <span className={styles.collapseCopy}>Agent Harness</span>
        <span
          className={styles.connectionStatus}
          data-status={connectionStatus}
          role="status"
          aria-label={connectionLabel}
          title={connectionLabel}
        >
          <span
            className={styles.connectionMark}
            data-connection-icon={connectionStatus}
            aria-hidden="true"
          />
          <span className={styles.connectionCopy}>{connectionCopy}</span>
        </span>
      </div>
      <button
        type="button"
        className={styles.primaryAction}
        aria-label="New chat"
        aria-keyshortcuts="Meta+N"
        onClick={() => void onCreate()}
      >
        <span className={styles.glyph} aria-hidden="true">+</span>
        <span className={styles.collapseCopy}>New chat</span>
        <kbd className={styles.shortcut}>⌘N</kbd>
      </button>
      <button
        type="button"
        className={styles.utilityAction}
        aria-label="Search sessions"
        aria-keyshortcuts="Meta+K"
        onClick={onSearch}
      >
        <span className={styles.glyph} aria-hidden="true">/</span>
        <span className={styles.collapseCopy}>Search sessions</span>
        <kbd className={styles.shortcut}>⌘K</kbd>
      </button>

      <div className={styles.sessionGroups}>
        {(Object.keys(groups) as GroupName[]).map((name) =>
          groups[name].length === 0 ? null : (
            <section key={name} aria-labelledby={`session-group-${name.toLowerCase()}`}>
              <h2 id={`session-group-${name.toLowerCase()}`} className={styles.groupHeading}>
                {name}
              </h2>
              <ul className={styles.sessionList}>
                {groups[name].map((session) => (
                  <SessionRow
                    key={session.session_id}
                    session={session}
                    active={session.session_id === activeSessionId}
                    collapsed={collapsed}
                    runtime={runtimeBySession[session.session_id]}
                    onSelect={onSelect}
                    onRename={onRename}
                    onArchive={onArchive}
                  />
                ))}
              </ul>
            </section>
          ),
        )}
      </div>

      <div className={styles.footer}>
        <button
          type="button"
          className={styles.utilityAction}
          aria-label="Settings"
          title="Settings"
          aria-current={settingsActive ? "page" : undefined}
          data-active={settingsActive || undefined}
          onClick={onOpenSettings}
        >
          <span className={styles.glyph} aria-hidden="true">&#8801;</span>
          <span className={styles.collapseCopy}>Settings</span>
        </button>
        <button
          type="button"
          className={styles.collapseButton}
          aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"}
          onClick={() => onCollapsedChange(!collapsed)}
        >
          <span className={styles.glyph} aria-hidden="true">{collapsed ? "\u203a" : "\u2039"}</span>
        </button>
      </div>
      </nav>
    </Tooltip.Provider>
  );
}
