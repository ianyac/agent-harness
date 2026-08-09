import * as Dialog from "@radix-ui/react-dialog";
import * as DropdownMenu from "@radix-ui/react-dropdown-menu";
import { Activity, ChevronDown, GitBranch, ShieldCheck } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import type { BaseMode, SetSessionMode } from "../../protocol/types";
import { workspaceName } from "./SessionRow";
import styles from "./sessionSidebar.module.css";

type ConversationHeaderProps = {
  readonly sessionId: string;
  readonly workspace: string;
  readonly branch: string | null;
  readonly mode: BaseMode;
  readonly onSetSessionMode: (event: SetSessionMode) => void;
  readonly onToggleActivity: () => void;
};

const modeLabels: Record<BaseMode, string> = {
  default: "Default",
  acceptAll: "Accept all",
  readOnly: "Read only",
};

export function ConversationHeader({
  sessionId,
  workspace,
  branch,
  mode,
  onSetSessionMode,
  onToggleActivity,
}: ConversationHeaderProps) {
  const [confirmation, setConfirmation] = useState<{
    readonly sessionId: string;
    readonly submit: (event: SetSessionMode) => void;
  } | null>(null);
  const modeTriggerRef = useRef<HTMLButtonElement>(null);
  const cancelRef = useRef<HTMLButtonElement>(null);
  const confirmationOpen = confirmation?.sessionId === sessionId;

  useEffect(() => {
    if (confirmation !== null && confirmation.sessionId !== sessionId) {
      setConfirmation(null);
    }
  }, [confirmation, sessionId]);

  const chooseMode = (nextMode: BaseMode) => {
    if (nextMode === mode) return;
    if (nextMode === "acceptAll") {
      setConfirmation({ sessionId, submit: onSetSessionMode });
      return;
    }
    onSetSessionMode({ type: "set_session_mode", mode: nextMode });
  };

  return (
    <header className={`conversation-header ${styles.header}`}>
      <div className={styles.context}>
        <h1 title={workspace}>{workspaceName(workspace)}</h1>
        {branch === null ? null : (
          <span className={styles.branch}>
            <GitBranch aria-hidden="true" size={15} />
            {branch}
          </span>
        )}
      </div>
      <div className={styles.headerActions}>
        <DropdownMenu.Root>
          <DropdownMenu.Trigger asChild>
            <button
              ref={modeTriggerRef}
              type="button"
              className={styles.headerButton}
              aria-label={`Permission mode: ${modeLabels[mode]}`}
            >
              <ShieldCheck aria-hidden="true" size={17} />
              {modeLabels[mode]}
              <ChevronDown aria-hidden="true" size={14} />
            </button>
          </DropdownMenu.Trigger>
          <DropdownMenu.Portal>
            <DropdownMenu.Content className={styles.menu} align="end" sideOffset={6}>
              <DropdownMenu.RadioGroup value={mode} onValueChange={(value) => chooseMode(value as BaseMode)}>
                <DropdownMenu.RadioItem className={styles.menuItem} value="default">
                  <DropdownMenu.ItemIndicator className={styles.itemIndicator} aria-hidden="true">✓</DropdownMenu.ItemIndicator>
                  Default
                </DropdownMenu.RadioItem>
                <DropdownMenu.RadioItem className={styles.menuItem} value="acceptAll">
                  <DropdownMenu.ItemIndicator className={styles.itemIndicator} aria-hidden="true">✓</DropdownMenu.ItemIndicator>
                  Accept all
                </DropdownMenu.RadioItem>
                <DropdownMenu.RadioItem className={styles.menuItem} value="readOnly">
                  <DropdownMenu.ItemIndicator className={styles.itemIndicator} aria-hidden="true">✓</DropdownMenu.ItemIndicator>
                  Read only
                </DropdownMenu.RadioItem>
              </DropdownMenu.RadioGroup>
            </DropdownMenu.Content>
          </DropdownMenu.Portal>
        </DropdownMenu.Root>
        <button type="button" className={styles.headerButton} onClick={onToggleActivity}>
          <Activity aria-hidden="true" size={17} />
          Activity
        </button>
      </div>

      <Dialog.Root
        open={confirmationOpen}
        onOpenChange={(nextOpen) => {
          if (!nextOpen) setConfirmation(null);
        }}
      >
        <Dialog.Portal>
          <Dialog.Overlay className={styles.overlay} />
          <Dialog.Content
            className={styles.confirmDialog}
            onOpenAutoFocus={(event) => {
              event.preventDefault();
              cancelRef.current?.focus();
            }}
            onCloseAutoFocus={(event) => {
              event.preventDefault();
              modeTriggerRef.current?.focus();
            }}
            onKeyDownCapture={(event) => {
              if (event.metaKey && ["k", "n"].includes(event.key.toLowerCase())) {
                event.preventDefault();
                event.stopPropagation();
              }
            }}
          >
            <Dialog.Title className={styles.dialogTitle}>Enable accept all?</Dialog.Title>
            <Dialog.Description className={styles.dialogDescription}>
              Mutating tools will run without per-call prompts for this session.
            </Dialog.Description>
            <div className={styles.dialogActions}>
              <Dialog.Close asChild>
                <button ref={cancelRef} type="button" className={styles.secondaryButton}>Cancel</button>
              </Dialog.Close>
              <button
                type="button"
                className={styles.dangerButton}
                onClick={() => {
                  if (confirmation?.sessionId === sessionId) {
                    confirmation.submit({ type: "set_session_mode", mode: "acceptAll" });
                  }
                  setConfirmation(null);
                }}
              >
                Enable accept all
              </button>
            </div>
          </Dialog.Content>
        </Dialog.Portal>
      </Dialog.Root>
    </header>
  );
}
