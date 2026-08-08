import * as Dialog from "@radix-ui/react-dialog";
import { Activity, MessageSquarePlus, Search, Settings } from "lucide-react";
import { useEffect, useMemo, useState } from "react";

import type { SessionRecord } from "../features/sessions/useSessions";
import { workspaceName } from "../features/sessions/SessionRow";
import styles from "../features/sessions/sessionSidebar.module.css";

type CommandPaletteProps = {
  readonly open: boolean;
  readonly onOpenChange: (open: boolean) => void;
  readonly sessions: readonly SessionRecord[];
  readonly onNewChat: () => void | Promise<void>;
  readonly onOpenSettings: () => void;
  readonly onToggleActivity: () => void;
  readonly onSelectSession: (sessionId: string) => void;
};

type ActionCommand = {
  readonly id: string;
  readonly label: string;
  readonly shortcut?: string;
  readonly icon: typeof Search;
  readonly run: () => void;
};

export function CommandPalette({
  open,
  onOpenChange,
  sessions,
  onNewChat,
  onOpenSettings,
  onToggleActivity,
  onSelectSession,
}: CommandPaletteProps) {
  const [query, setQuery] = useState("");

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (!event.metaKey || event.ctrlKey || event.altKey || event.shiftKey || event.repeat) return;
      const key = event.key.toLowerCase();
      if (key === "k") {
        event.preventDefault();
        onOpenChange(true);
      } else if (key === "n") {
        event.preventDefault();
        onOpenChange(false);
        void onNewChat();
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [onNewChat, onOpenChange]);

  useEffect(() => {
    if (!open) setQuery("");
  }, [open]);

  const runAndClose = (command: () => void) => {
    onOpenChange(false);
    command();
  };

  const actions: ActionCommand[] = [
    {
      id: "action:new-chat",
      label: "New chat",
      shortcut: "⌘N",
      icon: MessageSquarePlus,
      run: () => void onNewChat(),
    },
    {
      id: "action:open-settings",
      label: "Open settings",
      icon: Settings,
      run: onOpenSettings,
    },
    {
      id: "action:toggle-activity",
      label: "Toggle activity",
      shortcut: "⌘⇧I",
      icon: Activity,
      run: onToggleActivity,
    },
  ];
  const normalizedQuery = query.trim().toLocaleLowerCase();
  const matchingActions = actions.filter((action) =>
    action.label.toLocaleLowerCase().includes(normalizedQuery),
  );
  const matchingSessions = useMemo(
    () =>
      sessions.filter(
        (session) =>
          session.archived_at === null &&
          `${session.title}\n${session.workspace}`.toLocaleLowerCase().includes(normalizedQuery),
      ),
    [normalizedQuery, sessions],
  );

  return (
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
      <Dialog.Portal>
        <Dialog.Overlay className={styles.overlay} />
        <Dialog.Content className={styles.palette} aria-describedby={undefined}>
          <Dialog.Title className={styles.srOnly}>Command palette</Dialog.Title>
          <div className={styles.searchField}>
            <Search aria-hidden="true" size={18} />
            <input
              type="search"
              autoFocus
              aria-label="Search commands and sessions"
              placeholder="Search commands and sessions"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
            />
          </div>
          <div className={styles.commandList}>
            {matchingActions.length > 0 ? (
              <section aria-labelledby="command-actions-heading">
                <h2 id="command-actions-heading" className={styles.commandHeading}>Actions</h2>
                {matchingActions.map((action) => {
                  const Icon = action.icon;
                  return (
                    <button
                      key={action.id}
                      type="button"
                      className={styles.command}
                      data-command-id={action.id}
                      aria-keyshortcuts={
                        action.id === "action:new-chat"
                          ? "Meta+N"
                          : action.id === "action:toggle-activity"
                            ? "Meta+Shift+I"
                            : undefined
                      }
                      onClick={() => runAndClose(action.run)}
                    >
                      <Icon aria-hidden="true" size={17} />
                      <span>{action.label}</span>
                      {action.shortcut === undefined ? null : <kbd>{action.shortcut}</kbd>}
                    </button>
                  );
                })}
              </section>
            ) : null}
            {matchingSessions.length > 0 ? (
              <section aria-labelledby="command-sessions-heading">
                <h2 id="command-sessions-heading" className={styles.commandHeading}>Sessions</h2>
                {matchingSessions.map((session) => (
                  <button
                    key={session.session_id}
                    type="button"
                    className={styles.command}
                    data-command-id={`session:${session.session_id}`}
                    onClick={() => runAndClose(() => onSelectSession(session.session_id))}
                  >
                    <Search aria-hidden="true" size={17} />
                    <span>
                      {session.title}
                      <small>{workspaceName(session.workspace)}</small>
                    </span>
                  </button>
                ))}
              </section>
            ) : null}
            {matchingActions.length === 0 && matchingSessions.length === 0 ? (
              <p className={styles.emptyCommands}>No matching commands or sessions.</p>
            ) : null}
          </div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
