import { RefreshCw, X } from "lucide-react";
import { useRef, useState } from "react";

import { ApiError } from "../../api/http";
import type { SessionCleanupError, SessionOperationError } from "./useSessions";
import styles from "../../components/recovery.module.css";

type SessionOperationRecoveryProps = {
  readonly failure: SessionOperationError | SessionCleanupError;
  readonly onRetry: () => void | Promise<void>;
  readonly onDismiss?: () => void;
};

function operationName(kind: SessionOperationError["kind"] | SessionCleanupError["kind"]): string {
  if (kind === "create") return "New chat";
  if (kind === "rename") return "Rename";
  if (kind === "cleanup") return "Cleanup";
  return "Archive";
}

function copy(
  failure: SessionOperationError | SessionCleanupError,
): { readonly title: string; readonly body: string } {
  if (failure.kind === "cleanup") {
    return {
      title: "Cleanup needs another try",
      body: "An unused session is hidden until cleanup is confirmed.",
    };
  }
  const category = failure.error instanceof ApiError ? failure.error.category : "unknown";
  if (category === "credential_prerequisite") {
    return { title: "Sign in required", body: "Run codex login, then retry." };
  }
  if (category === "invalid_workspace" || category === "missing_workspace") {
    return { title: "Workspace unavailable", body: "The selected workspace is unavailable." };
  }
  if (failure.kind === "create") {
    return { title: "New chat wasn’t created", body: "Your current session was not changed." };
  }
  if (failure.kind === "rename") {
    return { title: "Session wasn’t renamed", body: "The previous session name is still in use." };
  }
  return { title: "Session wasn’t archived", body: "The session remains available." };
}

export function SessionOperationRecovery({
  failure,
  onRetry,
  onDismiss,
}: SessionOperationRecoveryProps) {
  const [pending, setPending] = useState(false);
  const pendingRef = useRef(false);
  const safeCopy = copy(failure);
  const label = operationName(failure.kind);

  const retry = async () => {
    if (pendingRef.current) return;
    pendingRef.current = true;
    setPending(true);
    try {
      await onRetry();
    } finally {
      pendingRef.current = false;
      setPending(false);
    }
  };

  return (
    <div className={styles.operationBanner} role="alert" aria-label="Session operation failed">
      <p className={styles.operationCopy}>
        <strong>{safeCopy.title}</strong>
        <span>{safeCopy.body}</span>
      </p>
      <div className={styles.operationActions}>
        <button type="button" disabled={pending} onClick={() => void retry()}>
          <RefreshCw aria-hidden="true" size={15} /> Retry {label}
        </button>
        {onDismiss === undefined ? null : (
          <button type="button" onClick={onDismiss}>
            <X aria-hidden="true" size={15} /> Dismiss
          </button>
        )}
      </div>
    </div>
  );
}
