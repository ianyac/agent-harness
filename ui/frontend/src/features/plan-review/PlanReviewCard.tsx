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
  readonly turnRunning?: boolean;
  readonly onAnswer: (event: PlanAnswer) => void | Promise<void>;
};

type SubmissionState = "idle" | "sending" | "submitted";

type SubmissionOperation = {
  readonly requestId: string;
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
  turnRunning = true,
  onAnswer,
}: PlanReviewCardProps) {
  const cardRef = useRef<HTMLElement>(null);
  const focusOrigin = useRef<HTMLElement | null>(null);
  const focusedRequest = useRef<string | null>(null);
  const pendingLock = useRef(false);
  const operationRef = useRef<SubmissionOperation | null>(null);
  const renderedRequest = useRef(request.requestId);
  const [revising, setRevising] = useState(false);
  const [feedback, setFeedback] = useState("");
  const [submission, setSubmission] = useState<SubmissionState>("idle");
  const [error, setError] = useState("");
  const actionable = turnRunning && active && resolution === null;
  const terminalUnresolved = !turnRunning && resolution === null;

  useLayoutEffect(() => {
    if (!actionable || focusedRequest.current === request.requestId) return;
    const card = cardRef.current;
    if (card === null) return;
    const current = document.activeElement;
    focusOrigin.current = current instanceof HTMLElement && !card.contains(current) ? current : null;
    focusedRequest.current = request.requestId;
    card.focus();
  }, [actionable, request.requestId]);

  useLayoutEffect(() => {
    if (renderedRequest.current !== request.requestId) {
      renderedRequest.current = request.requestId;
      operationRef.current = null;
      pendingLock.current = false;
      setSubmission("idle");
      setError("");
      setRevising(false);
      setFeedback("");
    }

    if (resolution === null && active && turnRunning) return;
    const submittedThisRequest = operationRef.current?.requestId === request.requestId;
    operationRef.current = null;
    pendingLock.current = false;
    setSubmission("idle");
    setError("");
    if (resolution !== null && submittedThisRequest) {
      const target = focusOrigin.current;
      focusOrigin.current = null;
      if (target?.isConnected) target.focus();
    }
  }, [active, request.requestId, resolution, turnRunning]);

  const submit = async (approved: boolean) => {
    if (!actionable || pendingLock.current) return;
    const operation: SubmissionOperation = { requestId: request.requestId };
    operationRef.current = operation;
    pendingLock.current = true;
    setSubmission("sending");
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
      if (operationRef.current !== operation) return;
      setSubmission("submitted");
      cardRef.current?.focus();
    } catch (reason) {
      if (operationRef.current !== operation) return;
      operationRef.current = null;
      pendingLock.current = false;
      setSubmission("idle");
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
            disabled={submission !== "idle"}
            autoFocus
            onChange={(event) => setFeedback(event.currentTarget.value)}
          />
          <div className={styles.actions}>
            <button type="button" className={styles.primary} disabled={submission !== "idle"} onClick={() => void submit(false)}>
              Send revision
            </button>
            <button
              type="button"
              disabled={submission !== "idle"}
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
          <button type="button" className={styles.primary} disabled={submission !== "idle"} onClick={() => void submit(true)}>
            Approve plan
          </button>
          <button type="button" disabled={submission !== "idle"} onClick={() => setRevising(true)}>
            Revise plan
          </button>
        </div>
      ) : terminalUnresolved ? (
        <p className={styles.waiting} role="status" aria-label="Plan review unresolved">
          Turn ended without a recorded decision.
        </p>
      ) : (
        <p className={styles.waiting}>Awaiting an authoritative decision</p>
      )}

      {resolution === null && actionable && submission !== "idle" ? (
        <p className={styles.pending} role="status" aria-label="Plan submission">
          {submission === "sending" ? "Sending decision…" : "Decision sent. Waiting for confirmation."}
        </p>
      ) : null}
      {error !== "" ? <p className={styles.error} role="alert">{error}</p> : null}
      {actionable ? (
        <span className={styles.srOnly} role="status" aria-label="Plan review request" aria-live="assertive">
          Plan review requested
        </span>
      ) : null}
    </section>
  );
}
