import * as DropdownMenu from "@radix-ui/react-dropdown-menu";
import { Circle, MoreHorizontal, Save, X } from "lucide-react";
import { useState } from "react";

import type { SessionRecord, SessionRuntimeState } from "./useSessions";
import styles from "./sessionSidebar.module.css";

type SessionRowProps = {
  readonly session: SessionRecord;
  readonly active: boolean;
  readonly runtime?: SessionRuntimeState;
  readonly onSelect: (sessionId: string) => void;
  readonly onRename: (sessionId: string, title: string) => void | Promise<void>;
  readonly onArchive: (sessionId: string) => void | Promise<void>;
};

function workspaceName(path: string): string {
  const trimmed = path.replace(/[\\/]+$/, "");
  return trimmed.split(/[\\/]/).at(-1) || path;
}

function statusText(runtime: SessionRuntimeState | undefined, active: boolean): string | null {
  if (runtime === undefined || runtime.status === "idle") return null;
  const labels: Record<SessionRuntimeState["status"], string> = {
    idle: "Idle",
    running: "Running",
    waiting_permission: "Waiting for permission",
    stopping: "Stopping",
    complete: "Complete",
    error: "Error",
  };
  const label = labels[runtime.status];
  return !active && ["running", "waiting_permission", "stopping"].includes(runtime.status)
    ? `${label} in background`
    : label;
}

export function SessionRow({
  session,
  active,
  runtime,
  onSelect,
  onRename,
  onArchive,
}: SessionRowProps) {
  const [renaming, setRenaming] = useState(false);
  const [draft, setDraft] = useState(session.title);
  const workspace = workspaceName(session.workspace);
  const status = statusText(runtime, active);
  const accessibleName = [session.title, `Workspace ${session.workspace}`, status]
    .filter(Boolean)
    .join(", ");

  const save = () => {
    const title = draft.trim();
    if (title === "" || title === session.title) {
      setDraft(session.title);
      setRenaming(false);
      return;
    }
    void Promise.resolve(onRename(session.session_id, title)).then(() => setRenaming(false));
  };

  return (
    <li className={styles.row} data-active={active || undefined}>
      {renaming ? (
        <form
          className={styles.renameForm}
          onSubmit={(event) => {
            event.preventDefault();
            save();
          }}
        >
          <label className={styles.srOnly} htmlFor={`rename-${session.session_id}`}>
            Rename {session.title}
          </label>
          <input
            id={`rename-${session.session_id}`}
            autoFocus
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
          />
          <button type="submit" aria-label="Save name">
            <Save aria-hidden="true" size={15} />
          </button>
          <button
            type="button"
            aria-label="Cancel rename"
            onClick={() => {
              setDraft(session.title);
              setRenaming(false);
            }}
          >
            <X aria-hidden="true" size={15} />
          </button>
        </form>
      ) : (
        <>
          <button
            type="button"
            className={styles.sessionButton}
            aria-label={accessibleName}
            aria-current={active ? "page" : undefined}
            onClick={() => onSelect(session.session_id)}
          >
            <Circle
              className={styles.statusIcon}
              data-status={runtime?.status ?? "idle"}
              aria-hidden="true"
              size={12}
              fill="currentColor"
            />
            <span className={styles.sessionCopy} aria-hidden="true">
              <span className={styles.sessionTitle}>{session.title}</span>
              <span className={styles.workspace}>{workspace}</span>
              {status === null ? null : <span className={styles.statusText}>{status}</span>}
            </span>
          </button>
          <DropdownMenu.Root>
            <DropdownMenu.Trigger asChild>
              <button
                type="button"
                className={styles.moreButton}
                aria-label={`More actions for ${session.title}`}
              >
                <MoreHorizontal aria-hidden="true" size={17} />
              </button>
            </DropdownMenu.Trigger>
            <DropdownMenu.Portal>
              <DropdownMenu.Content className={styles.menu} sideOffset={4} align="start">
                <DropdownMenu.Item
                  className={styles.menuItem}
                  onSelect={() => setRenaming(true)}
                >
                  Rename
                </DropdownMenu.Item>
                <DropdownMenu.Item
                  className={`${styles.menuItem} ${styles.dangerItem}`}
                  onSelect={() => void onArchive(session.session_id)}
                >
                  Archive
                </DropdownMenu.Item>
              </DropdownMenu.Content>
            </DropdownMenu.Portal>
          </DropdownMenu.Root>
        </>
      )}
    </li>
  );
}

export { workspaceName };
