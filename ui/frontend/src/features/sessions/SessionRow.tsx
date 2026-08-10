import * as Dialog from "@radix-ui/react-dialog";
import * as DropdownMenu from "@radix-ui/react-dropdown-menu";
import * as Tooltip from "@radix-ui/react-tooltip";
import { Circle, MoreHorizontal, Save, X } from "lucide-react";
import { useRef, useState } from "react";

import type { SessionRecord, SessionRuntimeState } from "./useSessions";
import styles from "./sessionSidebar.module.css";

type SessionRowProps = {
  readonly session: SessionRecord;
  readonly active: boolean;
  readonly collapsed: boolean;
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
  collapsed,
  runtime,
  onSelect,
  onRename,
  onArchive,
}: SessionRowProps) {
  const [renaming, setRenaming] = useState(false);
  const [draft, setDraft] = useState(session.title);
  const moreButtonRef = useRef<HTMLButtonElement>(null);
  const renameInputRef = useRef<HTMLInputElement>(null);
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

  const changeRenameOpen = (open: boolean) => {
    setDraft(session.title);
    setRenaming(open);
  };

  const sessionButton = (
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
  );

  return (
    <li className={styles.row} data-active={active || undefined}>
      {collapsed ? (
        <Tooltip.Root>
          <Tooltip.Trigger asChild>{sessionButton}</Tooltip.Trigger>
          <Tooltip.Portal>
            <Tooltip.Content className={styles.sessionTooltip} side="right" sideOffset={8}>
              <strong>{session.title}</strong>
              <span>{session.workspace}</span>
              {status === null ? null : <span>{status}</span>}
            </Tooltip.Content>
          </Tooltip.Portal>
        </Tooltip.Root>
      ) : (
        sessionButton
      )}
      <Dialog.Root open={renaming} onOpenChange={changeRenameOpen}>
        <DropdownMenu.Root>
          <DropdownMenu.Trigger asChild>
            <button
              ref={moreButtonRef}
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
                onSelect={() => changeRenameOpen(true)}
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
        <Dialog.Portal>
          <Dialog.Overlay className={styles.overlay} />
          <Dialog.Content
            className={styles.renameDialog}
            onOpenAutoFocus={(event) => {
              event.preventDefault();
              renameInputRef.current?.focus();
            }}
            onCloseAutoFocus={(event) => {
              event.preventDefault();
              moreButtonRef.current?.focus();
            }}
          >
            <Dialog.Title className={styles.dialogTitle}>Rename {session.title}</Dialog.Title>
            <Dialog.Description className={styles.dialogDescription}>
              Choose a new name for this session.
            </Dialog.Description>
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
                ref={renameInputRef}
                id={`rename-${session.session_id}`}
                value={draft}
                onChange={(event) => setDraft(event.target.value)}
              />
              <button type="submit" aria-label="Save name">
                <Save aria-hidden="true" size={15} />
              </button>
              <Dialog.Close asChild>
                <button type="button" aria-label="Cancel rename">
                  <X aria-hidden="true" size={15} />
                </button>
              </Dialog.Close>
            </form>
          </Dialog.Content>
        </Dialog.Portal>
      </Dialog.Root>
    </li>
  );
}

export { workspaceName };
