import * as Tooltip from "@radix-ui/react-tooltip";
import {
  Bot,
  ChevronLeft,
  ChevronRight,
  CircleAlert,
  CircleCheck,
  LoaderCircle,
  MessageSquarePlus,
  Search,
  Settings,
} from "lucide-react";
import { useState } from "react";

import { SessionRow } from "./SessionRow";
import type { SessionRecord, SessionRuntimeState } from "./useSessions";
import styles from "./sessionSidebar.module.css";

const collapsePreferenceKey = "harness.sidebar.collapsed";

type GroupName = "Today" | "Yesterday" | "Earlier";

export type ConnectionStatus = "connecting" | "connected" | "disconnected";

type SessionSidebarProps = {
  readonly sessions: readonly SessionRecord[];
  readonly activeSessionId: string | null;
  readonly runtimeBySession: Readonly<Record<string, SessionRuntimeState>>;
  readonly connectionStatus?: ConnectionStatus;
  readonly now?: Date;
  readonly storage?: Pick<Storage, "getItem" | "setItem">;
  readonly collapsed?: boolean;
  readonly onCollapsedChange?: (collapsed: boolean) => void;
  readonly onCreate: () => void | Promise<unknown>;
  readonly onSelect: (sessionId: string) => void;
  readonly onRename: (sessionId: string, title: string) => void | Promise<void>;
  readonly onArchive: (sessionId: string) => void | Promise<void>;
  readonly onSearch: () => void;
  readonly onOpenSettings: () => void;
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

function browserStorage(): Pick<Storage, "getItem" | "setItem"> | undefined {
  try {
    return window.localStorage;
  } catch {
    return undefined;
  }
}

function initialCollapsed(storage: Pick<Storage, "getItem" | "setItem"> | undefined): boolean {
  try {
    return storage?.getItem(collapsePreferenceKey) === "true";
  } catch {
    return false;
  }
}

export function SessionSidebar({
  sessions,
  activeSessionId,
  runtimeBySession,
  connectionStatus = "connecting",
  now = new Date(),
  storage,
  collapsed: controlledCollapsed,
  onCollapsedChange,
  onCreate,
  onSelect,
  onRename,
  onArchive,
  onSearch,
  onOpenSettings,
}: SessionSidebarProps) {
  const effectiveStorage = storage ?? (controlledCollapsed === undefined ? browserStorage() : undefined);
  const [localCollapsed, setLocalCollapsed] = useState(() => initialCollapsed(effectiveStorage));
  const collapsed = controlledCollapsed ?? localCollapsed;
  const visibleSessions = sessions.filter((session) => session.archived_at === null);
  const groups: Record<GroupName, SessionRecord[]> = {
    Today: [],
    Yesterday: [],
    Earlier: [],
  };
  for (const session of visibleSessions) groups[groupName(session, now)].push(session);

  const toggleCollapsed = () => {
    const next = !collapsed;
    if (controlledCollapsed === undefined) {
      setLocalCollapsed(next);
      try {
        effectiveStorage?.setItem(collapsePreferenceKey, String(next));
      } catch {
        // Preference storage is optional; the current view remains usable.
      }
    }
    onCollapsedChange?.(next);
  };
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
  const ConnectionIcon = {
    connecting: LoaderCircle,
    connected: CircleCheck,
    disconnected: CircleAlert,
  }[connectionStatus];

  return (
    <Tooltip.Provider delayDuration={0} skipDelayDuration={0}>
      <nav
        aria-label="Sessions"
        className={`session-sidebar ${styles.sidebar}`}
        data-collapsed={collapsed}
      >
      <div className={styles.identity}>
        <Bot aria-hidden="true" size={20} />
        <span className={styles.collapseCopy}>Agent Harness</span>
        <span
          className={styles.connectionStatus}
          data-status={connectionStatus}
          role="status"
          aria-label={connectionLabel}
          title={connectionLabel}
        >
          <ConnectionIcon
            className={styles.connectionMark}
            data-connection-icon={connectionStatus}
            aria-hidden="true"
            size={16}
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
        <MessageSquarePlus aria-hidden="true" size={18} />
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
        <Search aria-hidden="true" size={18} />
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
          onClick={onOpenSettings}
        >
          <Settings aria-hidden="true" size={18} />
          <span className={styles.collapseCopy}>Settings</span>
        </button>
        <button
          type="button"
          className={styles.collapseButton}
          aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"}
          onClick={toggleCollapsed}
        >
          {collapsed ? (
            <ChevronRight aria-hidden="true" size={18} />
          ) : (
            <ChevronLeft aria-hidden="true" size={18} />
          )}
        </button>
      </div>
      </nav>
    </Tooltip.Provider>
  );
}
