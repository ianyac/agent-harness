import { Archive, FolderOpen, Logs, Power, RefreshCw } from "lucide-react";
import { useRef, useState } from "react";

import type { PlatformAdapter } from "../platform/types";
import type { RecoverableError } from "../protocol/types";
import styles from "./recovery.module.css";

type RecoveryViewProps = {
  readonly error: RecoverableError;
  readonly sessionId: string;
  readonly platform: PlatformAdapter;
  readonly onLocate: (sessionId: string, workspace: string) => void | Promise<void>;
  readonly onArchive: (sessionId: string) => void | Promise<void>;
  readonly onRetry?: () => void | Promise<void>;
};

const copyByCategory: Readonly<Record<string, { readonly title: string; readonly body: string }>> = {
  credential_prerequisite: {
    title: "Sign in required",
    body: "Run codex login, then retry.",
  },
  invalid_workspace: {
    title: "Workspace unavailable",
    body: "The workspace is unavailable.",
  },
  missing_workspace: {
    title: "Workspace unavailable",
    body: "The workspace is unavailable.",
  },
  session_resume_failure: {
    title: "Session could not reopen",
    body: "This session could not be reopened.",
  },
  session_resume_error: {
    title: "Session could not reopen",
    body: "This session could not be reopened.",
  },
  turn_failure: {
    title: "Turn stopped",
    body: "The turn stopped before it completed.",
  },
  sidecar_second_crash: {
    title: "Local service stopped",
    body: "The local service stopped again.",
  },
};

const genericCopy = {
  title: "Recovery needed",
  body: "Something went wrong. Your local session data was not changed.",
};

export function RecoveryView({
  error,
  sessionId,
  platform,
  onLocate,
  onArchive,
  onRetry,
}: RecoveryViewProps) {
  const copy = copyByCategory[error.category] ?? genericCopy;
  const [pending, setPending] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const pendingRef = useRef(false);
  const operationRef = useRef(0);

  const run = async (action: () => void | Promise<void>) => {
    if (pendingRef.current) return;
    pendingRef.current = true;
    setPending(true);
    setActionError(null);
    const operation = ++operationRef.current;
    try {
      await action();
    } catch (value) {
      if (operationRef.current === operation) {
        setActionError(value instanceof Error ? value.message : "The recovery action failed.");
      }
    } finally {
      if (operationRef.current === operation) {
        pendingRef.current = false;
        setPending(false);
      }
    }
  };

  const locate = () => run(async () => {
    const chosen = await platform.chooseWorkspace();
    if (chosen !== null) await onLocate(sessionId, chosen);
  });
  const workspaceError = error.category === "invalid_workspace" || error.category === "missing_workspace";
  const crashed = error.category === "sidecar_second_crash";

  return (
    <section className={styles.recovery} role="alert" aria-labelledby="recovery-title">
      <h1 id="recovery-title">{copy.title}</h1>
      <p>{copy.body}</p>
      <div className={styles.actions}>
        {workspaceError ? (
          <>
            <button type="button" disabled={pending} onClick={() => void locate()}>
              <FolderOpen aria-hidden="true" size={18} /> Locate workspace
            </button>
            <button type="button" disabled={pending} onClick={() => void run(() => onArchive(sessionId))}>
              <Archive aria-hidden="true" size={18} /> Archive session
            </button>
          </>
        ) : null}
        {onRetry === undefined || ![
          "turn_failure",
          "session_resume_failure",
          "session_resume_error",
        ].includes(error.category) ? null : (
          <button type="button" disabled={pending} onClick={() => void run(onRetry)}>
            <RefreshCw aria-hidden="true" size={18} /> Retry
          </button>
        )}
        {crashed && platform.restartService !== undefined ? (
          <button type="button" disabled={pending} onClick={() => void run(platform.restartService!)}>
            <RefreshCw aria-hidden="true" size={18} /> Restart service
          </button>
        ) : null}
        {crashed && platform.openLogs !== undefined ? (
          <button type="button" disabled={pending} onClick={() => void run(platform.openLogs!)}>
            <Logs aria-hidden="true" size={18} /> Open logs
          </button>
        ) : null}
        {crashed && platform.quit !== undefined ? (
          <button type="button" disabled={pending} onClick={() => void run(platform.quit!)}>
            <Power aria-hidden="true" size={18} /> Quit
          </button>
        ) : null}
      </div>
      {actionError === null ? null : (
        <p className={styles.actionError} role="status" aria-label="Recovery action failed">
          {actionError}
        </p>
      )}
    </section>
  );
}
