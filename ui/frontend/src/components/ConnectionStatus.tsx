import { CircleCheck, LoaderCircle, RefreshCw } from "lucide-react";
import { useEffect, useState } from "react";

import styles from "./recovery.module.css";

export type ServiceConnectionStatus = "checking" | "reconnecting" | "connected" | "unrecoverable";

type ConnectionStatusProps = {
  readonly status: ServiceConnectionStatus;
  readonly attemptKey: unknown;
  readonly retrying?: boolean;
  readonly onRetry?: () => void;
};

const labels = {
  checking: "Local service checking",
  reconnecting: "Local service reconnecting",
  connected: "Local service connected",
} as const;

export function ConnectionStatus({
  status,
  attemptKey,
  retrying = false,
  onRetry,
}: ConnectionStatusProps) {
  const [prolonged, setProlonged] = useState(false);

  useEffect(() => {
    setProlonged(false);
    if (status !== "reconnecting") return;
    const timer = window.setTimeout(() => setProlonged(true), 10_000);
    return () => window.clearTimeout(timer);
  }, [attemptKey, status]);

  if (status === "unrecoverable") {
    return (
      <div className={styles.unrecoverableBanner} role="alert">
        <div>
          <strong>This tab lost its access to the local service</strong>
          <p>
            The access token lives only in this tab’s memory, so a page reload signs the
            tab out. Reopen the fresh link printed by the local service (restart it if
            needed) — your sessions and data are safe on disk.
          </p>
        </div>
        {onRetry === undefined ? null : (
          <button type="button" disabled={retrying} onClick={onRetry}>
            Retry
          </button>
        )}
      </div>
    );
  }

  const Icon = status === "checking"
    ? LoaderCircle
    : status === "reconnecting" ? RefreshCw : CircleCheck;
  const copy = status === "checking"
    ? "Checking"
    : status === "reconnecting" ? "Reconnecting" : "Connected";

  return (
    <>
      <span className={styles.connection} role="status" aria-label={labels[status]}>
        <Icon aria-hidden="true" size={16} /> {copy}
      </span>
      {status === "reconnecting" && prolonged ? (
        <div className={styles.reconnectBanner} role="alert">
          <span>Still reconnecting to the local service. Your local session data is unchanged.</span>
          {onRetry === undefined ? null : (
            <button type="button" disabled={retrying} onClick={onRetry}>
              {retrying ? "Retrying…" : "Retry connection"}
            </button>
          )}
        </div>
      ) : null}
    </>
  );
}
