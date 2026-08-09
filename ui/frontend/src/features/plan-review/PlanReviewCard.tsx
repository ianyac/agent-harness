import { ClipboardCheck } from "lucide-react";
import { useLayoutEffect, useRef, useState } from "react";

import type {
  PlanAnswer,
  PlanReviewRequest,
  PlanReviewResolution,
} from "../../protocol/types";
import { MarkdownContent } from "../conversation/MarkdownContent";
import styles from "./planReviewCard.module.css";

type PlanReviewCardProps = {
  readonly request: PlanReviewRequest;
  readonly resolution: PlanReviewResolution | null;
  readonly active: boolean;
  readonly onAnswer: (event: PlanAnswer) => void | Promise<void>;
};

function errorCopy(error: unknown): string {
  return error instanceof Error && error.message.trim() !== ""
    ? error.message
    : "The plan answer could not be sent. Try again.";
}

export function PlanReviewCard({
  request,
  resolution,
  active,
  onAnswer,
}: PlanReviewCardProps) {
  const cardRef = useRef<HTMLElement>(null);
  const focusOrigin = useRef<HTMLElement | null>(null);
  const focusedRequest = useRef<string | null>(null);
  const pendingLock = useRef(false);
  const [revising, setRevising] = useState(false);
  const [feedback, setFeedback] = useState("");
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

  const submit = async (approved: boolean) => {
    if (!actionable || pendingLock.current) return;
    pendingLock.current = true;
    setPending(true);
    setError("");
    const trimmedFeedback = feedback.trim();
    const event: PlanAnswer = approved || trimmedFeedback === ""
      ? { type: "answer_plan", request_id: request.requestId, approved }
      : {
          type: "answer_plan",
          request_id: request.requestId,
          approved: false,
          feedback: trimmedFeedback,
        };
    try {
      await onAnswer(event);
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

  return (
    <section
      ref={cardRef}
      className={styles.card}
      role="group"
      aria-label="Plan review"
      tabIndex={actionable ? -1 : undefined}
      data-state={resolution === null ? actionable ? "active" : "waiting" : resolution.approved ? "approved" : "revision"}
    >
      <div className={styles.headingRow}>
        <ClipboardCheck aria-hidden="true" size={18} />
        <div>
          <h2>Plan review</h2>
          <p>Review the proposed approach before work continues.</p>
        </div>
      </div>

      <div className={styles.plan} aria-label="Proposed plan content">
        <MarkdownContent content={request.plan} />
      </div>

      {resolution !== null ? (
        <div className={styles.resolution} role="status" aria-label="Plan review resolved">
          <strong>{resolution.approved ? "Plan approved" : "Revision requested"}</strong>
          {!resolution.approved && resolution.feedback.trim() !== "" ? (
            <p>{resolution.feedback}</p>
          ) : null}
        </div>
      ) : actionable && revising ? (
        <div className={styles.revision}>
          <label htmlFor={`plan-feedback-${request.requestId}`}>Revision feedback (optional)</label>
          <textarea
            id={`plan-feedback-${request.requestId}`}
            value={feedback}
            rows={3}
            disabled={pending}
            autoFocus
            onChange={(event) => setFeedback(event.currentTarget.value)}
          />
          <div className={styles.actions}>
            <button type="button" className={styles.primary} disabled={pending} onClick={() => void submit(false)}>
              Send revision
            </button>
            <button
              type="button"
              disabled={pending}
              onClick={() => {
                setRevising(false);
                setError("");
                cardRef.current?.focus();
              }}
            >
              Cancel revision
            </button>
          </div>
        </div>
      ) : actionable ? (
        <div className={styles.actions}>
          <button type="button" className={styles.primary} disabled={pending} onClick={() => void submit(true)}>
            Approve plan
          </button>
          <button type="button" disabled={pending} onClick={() => setRevising(true)}>
            Revise plan
          </button>
        </div>
      ) : (
        <p className={styles.waiting}>Awaiting an authoritative decision</p>
      )}

      {pending ? <p className={styles.pending} role="status">Sending decision…</p> : null}
      {error !== "" ? <p className={styles.error} role="alert">{error}</p> : null}
      {actionable ? (
        <span className={styles.srOnly} role="status" aria-label="Plan review request" aria-live="assertive">
          Plan review requested
        </span>
      ) : null}
    </section>
  );
}
