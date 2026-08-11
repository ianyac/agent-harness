import * as Dialog from "@radix-ui/react-dialog";
import * as DropdownMenu from "@radix-ui/react-dropdown-menu";
import { useEffect, useRef, useState } from "react";

import type { BaseMode, JsonObject, SetSessionMode } from "../../protocol/types";
import { workspaceName } from "./SessionRow";
import styles from "./sessionSidebar.module.css";

type ConversationHeaderProps = {
  readonly sessionId: string;
  readonly workspace: string;
  readonly branch: string | null;
  readonly latestContext?: JsonObject | null;
  readonly mode: BaseMode;
  readonly onSetSessionMode: (event: SetSessionMode) => void;
  readonly onToggleActivity: () => void;
};

function contextStat(context: JsonObject): string | null {
  const parts: string[] = [];
  if (typeof context.used_tokens === "number" && Number.isSafeInteger(context.used_tokens) && context.used_tokens >= 0) {
    parts.push(`${context.used_tokens.toLocaleString("en-US")} tok`);
  }
  if (typeof context.summarized_messages === "number" && Number.isSafeInteger(context.summarized_messages) && context.summarized_messages > 0) {
    parts.push(`${context.summarized_messages} ${context.mode === "folding" ? "folded" : "compacted"}`);
  }
  return parts.length === 0 ? null : parts.join(" · ");
}

const modeLabels: Record<BaseMode, string> = {
  default: "Default",
  acceptAll: "Accept all",
  readOnly: "Read only",
};

export function ConversationHeader({
  sessionId,
  workspace,
  branch,
  latestContext = null,
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
            {branch}
          </span>
        )}
        {latestContext === null || contextStat(latestContext) === null ? null : (
          <span
            className={styles.contextStat}
            role="status"
            aria-label="Context management status"
            title="Live context management state for this session"
          >
            ctx {contextStat(latestContext)}
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
              {modeLabels[mode]}
              <span aria-hidden="true">&#9662;</span>
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
