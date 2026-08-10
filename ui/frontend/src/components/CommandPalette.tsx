import * as Dialog from "@radix-ui/react-dialog";
import { Activity, MessageSquarePlus, Search, Settings } from "lucide-react";
import { useEffect, useId, useMemo, useRef, useState } from "react";

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
  const [activeIndex, setActiveIndex] = useState(0);
  const focusOrigin = useRef<HTMLElement | null>(null);
  const searchInput = useRef<HTMLInputElement>(null);
  const listboxId = useId();

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (!event.metaKey || event.ctrlKey || event.altKey || event.shiftKey || event.repeat) return;
      const key = event.key.toLowerCase();
      if (key === "k") {
        event.preventDefault();
        if (!open && document.activeElement instanceof HTMLElement) {
          focusOrigin.current = document.activeElement;
        }
        onOpenChange(true);
      } else if (key === "n") {
        event.preventDefault();
        onOpenChange(false);
        void onNewChat();
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [onNewChat, onOpenChange, open]);

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
      sessions.filter((session) =>
        `${session.title}\n${session.workspace}`.toLocaleLowerCase().includes(normalizedQuery),
      ),
    [normalizedQuery, sessions],
  );
  const optionCount = matchingActions.length + matchingSessions.length;
  const activeOption = optionCount === 0 ? 0 : Math.min(activeIndex, optionCount - 1);

  useEffect(() => {
    setActiveIndex(0);
  }, [open, normalizedQuery, sessions]);

  const activateOption = (index: number) => {
    const action = matchingActions[index];
    if (action !== undefined) {
      runAndClose(action.run);
      return;
    }
    const session = matchingSessions[index - matchingActions.length];
    if (session !== undefined) runAndClose(() => onSelectSession(session.session_id));
  };

  return (
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
      <Dialog.Portal>
        <Dialog.Overlay className={styles.overlay} />
        <Dialog.Content
          className={styles.palette}
          aria-describedby={undefined}
          onOpenAutoFocus={(event) => {
            event.preventDefault();
            if (focusOrigin.current === null && document.activeElement instanceof HTMLElement) {
              focusOrigin.current = document.activeElement;
            }
            searchInput.current?.focus();
          }}
          onCloseAutoFocus={(event) => {
            event.preventDefault();
            const target = focusOrigin.current;
            focusOrigin.current = null;
            if (target?.isConnected) target.focus();
          }}
        >
          <Dialog.Title className={styles.srOnly}>Command palette</Dialog.Title>
          <div className={styles.searchField}>
            <Search aria-hidden="true" size={18} />
            <input
              ref={searchInput}
              type="search"
              aria-label="Search commands and sessions"
              aria-autocomplete="list"
              aria-controls={listboxId}
              aria-activedescendant={optionCount === 0 ? undefined : `${listboxId}-${activeOption}`}
              placeholder="Search commands and sessions"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              onKeyDown={(event) => {
                if (optionCount === 0) return;
                if (event.key === "ArrowDown") {
                  event.preventDefault();
                  setActiveIndex((activeOption + 1) % optionCount);
                } else if (event.key === "ArrowUp") {
                  event.preventDefault();
                  setActiveIndex((activeOption - 1 + optionCount) % optionCount);
                } else if (event.key === "Home") {
                  event.preventDefault();
                  setActiveIndex(0);
                } else if (event.key === "End") {
                  event.preventDefault();
                  setActiveIndex(optionCount - 1);
                } else if (event.key === "Enter") {
                  event.preventDefault();
                  activateOption(activeOption);
                }
              }}
            />
          </div>
          <div
            className={styles.commandList}
            role="listbox"
            aria-label="Commands and sessions"
            id={listboxId}
          >
            {matchingActions.length > 0 ? (
              <div role="group" aria-labelledby="command-actions-heading">
                <div id="command-actions-heading" className={styles.commandHeading}>Actions</div>
                {matchingActions.map((action, index) => {
                  const Icon = action.icon;
                  return (
                    <button
                      key={action.id}
                      id={`${listboxId}-${index}`}
                      type="button"
                      role="option"
                      aria-selected={index === activeOption}
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
              </div>
            ) : null}
            {matchingSessions.length > 0 ? (
              <div role="group" aria-labelledby="command-sessions-heading">
                <div id="command-sessions-heading" className={styles.commandHeading}>Sessions</div>
                {matchingSessions.map((session, index) => (
                  <button
                    key={session.session_id}
                    id={`${listboxId}-${matchingActions.length + index}`}
                    type="button"
                    role="option"
                    aria-selected={matchingActions.length + index === activeOption}
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
              </div>
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
