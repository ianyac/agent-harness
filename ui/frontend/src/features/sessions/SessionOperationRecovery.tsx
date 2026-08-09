import { RefreshCw } from "lucide-react";
import { useRef, useState } from "react";

import { ApiError } from "../../api/http";
import type { SessionCleanupError, SessionOperationError } from "./useSessions";
import styles from "../../components/recovery.module.css";

type SessionOperationRecoveryProps = {
  readonly failure: SessionOperationError | SessionCleanupError;
  readonly onRetry: () => void | Promise<void>;
};

function operationName(kind: SessionOperationError["kind"] | SessionCleanupError["kind"]): string {
  if (kind === "create") return "New chat";
  if (kind === "rename") return "Rename";
  if (kind === "cleanup") return "cleanup";
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

export function SessionOperationRecovery({ failure, onRetry }: SessionOperationRecoveryProps) {
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
    <section className={styles.recovery} role="alert" aria-label="Session operation failed">
      <h1>{safeCopy.title}</h1>
      <p>{safeCopy.body}</p>
      <div className={styles.actions}>
        <button type="button" disabled={pending} onClick={() => void retry()}>
          <RefreshCw aria-hidden="true" size={18} /> Retry {label}
        </button>
      </div>
    </section>
  );
}
