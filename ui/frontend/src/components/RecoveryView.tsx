import { Archive, Logs, Power, RefreshCw } from "lucide-react";
import { useRef, useState } from "react";

import type { PlatformAdapter } from "../platform/types";
import type { RecoverableError } from "../protocol/types";
import styles from "./recovery.module.css";

type RecoveryViewProps = {
  readonly error: RecoverableError;
  readonly sessionId: string;
  readonly platform: PlatformAdapter;
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
    title: "Turn failed",
    body: "The turn could not complete. The conversation was rolled back to the last completed message, and your draft is preserved.",
  },
  turn_cancelled: {
    title: "Turn stopped",
    body: "The turn stopped before it completed.",
  },
  sidecar_second_crash: {
    title: "Local service stopped",
    body: "The local service stopped again.",
  },
};

type TurnFailureNoticeProps = {
  readonly onRetry?: () => void | Promise<void>;
};

/**
 * Compact strip for turn-level failures; the transcript stays visible. Copy is
 * fixed because raw provider messages may leak internals.
 */
export function TurnFailureNotice({ onRetry }: TurnFailureNoticeProps) {
  const [pending, setPending] = useState(false);
  const [failed, setFailed] = useState(false);
  const pendingRef = useRef(false);

  const retry = async (action: () => void | Promise<void>) => {
    if (pendingRef.current) return;
    pendingRef.current = true;
    setPending(true);
    setFailed(false);
    try {
      await action();
    } catch {
      setFailed(true);
    } finally {
      pendingRef.current = false;
      setPending(false);
    }
  };

  return (
    <div className={styles.operationBanner} role="alert">
      <p className={styles.operationCopy}>
        <strong>Turn failed</strong>
        <span>
          The turn could not complete. The conversation was rolled back to the
          last completed message, and your draft is preserved.
        </span>
      </p>
      <div className={styles.operationActions}>
        {onRetry === undefined ? null : (
          <button type="button" disabled={pending} onClick={() => void retry(onRetry)}>
            <RefreshCw aria-hidden="true" size={18} /> Retry
          </button>
        )}
        {failed ? (
          <p className={styles.operationCopy} role="status" aria-label="Recovery action failed">
            The recovery action failed.
          </p>
        ) : null}
      </div>
    </div>
  );
}

export function RecoveryView({
  error,
  sessionId,
  platform,
  onArchive,
  onRetry,
}: RecoveryViewProps) {
  // Unknown categories behave exactly like turn_failure so callers can pass
  // raw provider categories through without pre-normalizing them.
  const category = error.category in copyByCategory ? error.category : "turn_failure";
  const copy = copyByCategory[category];
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
    } catch {
      if (operationRef.current === operation) {
        setActionError("The recovery action failed.");
      }
    } finally {
      if (operationRef.current === operation) {
        pendingRef.current = false;
        setPending(false);
      }
    }
  };

  const workspaceError = category === "invalid_workspace" || category === "missing_workspace";
  const crashed = category === "sidecar_second_crash";

  return (
    <section className={styles.recovery} role="alert" aria-labelledby="recovery-title">
      <h1 id="recovery-title">{copy.title}</h1>
      <p>{copy.body}</p>
      <div className={styles.actions}>
        {workspaceError ? (
          <button type="button" disabled={pending} onClick={() => void run(() => onArchive(sessionId))}>
            <Archive aria-hidden="true" size={18} /> Archive session
          </button>
        ) : null}
        {onRetry === undefined || ![
          "turn_failure",
          "session_resume_failure",
          "session_resume_error",
        ].includes(category) ? null : (
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
