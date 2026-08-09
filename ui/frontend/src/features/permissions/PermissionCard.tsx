import { Globe2, ShieldAlert } from "lucide-react";
import { useLayoutEffect, useRef, useState } from "react";
import type { KeyboardEvent } from "react";

import type {
  PermissionAnswer,
  PermissionDecision,
  PermissionRequest,
  PermissionResolution,
  SafetySnapshot,
} from "../../protocol/types";
import styles from "./permissionCard.module.css";

type PermissionCardProps = {
  readonly request: PermissionRequest;
  readonly resolution: PermissionResolution | null;
  readonly active: boolean;
  readonly safety: SafetySnapshot | null;
  readonly onAnswer: (event: PermissionAnswer) => void | Promise<void>;
};

function formattedArguments(scope: string): string {
  try {
    return JSON.stringify(JSON.parse(scope), null, 2);
  } catch {
    return scope;
  }
}

function hasNetworkEgress(safety: SafetySnapshot | null, action: string): boolean {
  const tools = safety?.tools;
  if (tools === null || tools === undefined || Array.isArray(tools) || typeof tools !== "object") {
    return false;
  }
  const tool = tools[action];
  return tool !== null && !Array.isArray(tool) && typeof tool === "object" &&
    tool.network_egress === true;
}

function resolutionCopy(answer: PermissionDecision): string {
  if (answer === "yes") return "Allowed once";
  if (answer === "always") return "Always allowed for this session";
  return "Denied";
}

function errorCopy(error: unknown): string {
  return error instanceof Error && error.message.trim() !== ""
    ? error.message
    : "The permission answer could not be sent. Try again.";
}

export function PermissionCard({
  request,
  resolution,
  active,
  safety,
  onAnswer,
}: PermissionCardProps) {
  const cardRef = useRef<HTMLElement>(null);
  const focusOrigin = useRef<HTMLElement | null>(null);
  const focusedRequest = useRef<string | null>(null);
  const pendingLock = useRef(false);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState("");
  const actionable = active && resolution === null;

  useLayoutEffect(() => {
    if (!actionable || focusedRequest.current === request.requestId) return;
    const card = cardRef.current;
    if (card === null) return;
    const current = document.activeElement;
    focusOrigin.current = current instanceof HTMLElement && !card.contains(current) ? current : null;
    focusedRequest.current = request.requestId;
    card.focus();
  }, [actionable, request.requestId]);

  const submit = async (answer: PermissionDecision) => {
    if (!actionable || pendingLock.current) return;
    pendingLock.current = true;
    setPending(true);
    setError("");
    try {
      await onAnswer({
        type: "answer_permission",
        request_id: request.requestId,
        answer,
      });
      pendingLock.current = false;
      setPending(false);
      const target = focusOrigin.current;
      focusOrigin.current = null;
      if (target?.isConnected) target.focus();
    } catch (reason) {
      pendingLock.current = false;
      setPending(false);
      setError(errorCopy(reason));
      cardRef.current?.focus();
    }
  };

  const onKeyDown = (event: KeyboardEvent<HTMLElement>) => {
    if (!actionable || pendingLock.current || event.repeat || event.metaKey || event.ctrlKey || event.altKey) {
      return;
    }
    const card = cardRef.current;
    if (card === null || !card.contains(document.activeElement)) return;
    const answer = event.key.toLocaleLowerCase() === "a"
      ? "yes"
      : event.key.toLocaleLowerCase() === "d"
        ? "no"
        : event.key.toLocaleLowerCase() === "s" ? "always" : null;
    if (answer === null) return;
    event.preventDefault();
    event.stopPropagation();
    void submit(answer);
  };

  return (
    <section
      ref={cardRef}
      className={styles.card}
      role="group"
      aria-label={actionable ? "Permission decision" : "Permission review"}
      tabIndex={actionable ? -1 : undefined}
      data-state={resolution === null ? actionable ? "active" : "waiting" : resolution.answer}
      onKeyDown={onKeyDown}
    >
      <div className={styles.headingRow}>
        <ShieldAlert aria-hidden="true" size={18} />
        <div>
          <h2>Permission required</h2>
          <p className={styles.action}>{request.action}</p>
        </div>
      </div>

      <dl className={styles.details}>
        <div>
          <dt>Arguments</dt>
          <dd>
            <pre aria-label="Permission arguments">{formattedArguments(request.scope)}</pre>
          </dd>
        </div>
        <div>
          <dt>Scope</dt>
          <dd><code className={styles.scope}>{request.scope}</code></dd>
        </div>
        <div>
          <dt>Reason</dt>
          <dd>{request.reason}</dd>
        </div>
      </dl>

      {hasNetworkEgress(safety, request.action) ? (
        <p className={styles.network}>
          <Globe2 aria-hidden="true" size={16} />
          <strong>Network access sends data outside the workspace.</strong>
        </p>
      ) : null}

      {resolution !== null ? (
        <p className={styles.resolution} role="status" aria-label="Permission resolved">
          {resolutionCopy(resolution.answer)}
        </p>
      ) : actionable ? (
        <div className={styles.actions} role="group" aria-label="Permission choices">
          <button
            type="button"
            className={styles.primary}
            disabled={pending}
            aria-keyshortcuts="A"
            onClick={() => void submit("yes")}
          >
            Allow once <kbd>A</kbd>
          </button>
          <button
            type="button"
            disabled={pending}
            aria-keyshortcuts="D"
            onClick={() => void submit("no")}
          >
            Deny <kbd>D</kbd>
          </button>
          <button
            type="button"
            disabled={pending}
            aria-keyshortcuts="S"
            onClick={() => void submit("always")}
          >
            Always for this session <kbd>S</kbd>
          </button>
        </div>
      ) : (
        <p className={styles.waiting}>Awaiting an authoritative decision</p>
      )}

      {pending ? <p className={styles.pending} role="status">Sending decision…</p> : null}
      {error !== "" ? <p className={styles.error} role="alert">{error}</p> : null}
      {actionable ? (
        <span className={styles.srOnly} role="status" aria-label="Permission request" aria-live="assertive">
          Permission required: {request.reason}
        </span>
      ) : null}
    </section>
  );
}
