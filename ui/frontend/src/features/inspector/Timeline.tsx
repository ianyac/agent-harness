import {
  Bot,
  CheckCircle2,
  CircleAlert,
  GitMerge,
  ListChecks,
  MessageSquareText,
  ShieldAlert,
  Wrench,
} from "lucide-react";
import type { CSSProperties } from "react";

import { messageHistory } from "../../protocol/history";
import type { ActivityItem, TranscriptState, TranscriptTimelineItem } from "../../protocol/types";
import styles from "./inspector.module.css";

function readable(value: string): string {
  return value.replaceAll("_", " ");
}

function sentenceCase(value: string): string {
  const readableValue = readable(value);
  return `${readableValue[0]?.toLocaleUpperCase() ?? ""}${readableValue.slice(1)}`;
}

function compactEvidence(value: string): string {
  const firstLine = value.split(/\r?\n/, 1)[0] ?? "";
  return firstLine.length > 96 ? `${firstLine.slice(0, 95)}…` : firstLine;
}

function activityDepth(activity: ActivityItem, activities: TranscriptState["activities"]): number {
  let depth = 1;
  let parentId = activity.parentActivityId;
  const visited = new Set([activity.activityId]);
  while (parentId !== null) {
    if (visited.has(parentId)) return 1;
    const parent = activities[parentId];
    if (parent === undefined) return 1;
    visited.add(parentId);
    depth += 1;
    parentId = parent.parentActivityId;
  }
  return depth;
}

function boundaryLabel(boundary: Extract<TranscriptTimelineItem, { kind: "boundary" }>["boundary"]): string {
  if (boundary === "turn_completion") return "Turn complete";
  if (boundary === "activity_error" || boundary === "error") return "Error";
  return "Subagent complete";
}

type TimelineProps = {
  readonly state: TranscriptState;
  readonly selectedActivityId: string | null;
  readonly onSelectActivity: (activityId: string) => void;
};

export function Timeline({ state, selectedActivityId, onSelectActivity }: TimelineProps) {
  const historical = messageHistory(state.messages).groups
    .flatMap((group) => group.activities);
  return (
    <section className={styles.timelineSection} aria-labelledby="inspector-timeline-title">
      <h3 id="inspector-timeline-title">Timeline</h3>
      {state.timeline.length === 0 && historical.length === 0 ? (
        <p className={styles.empty}>No activity recorded yet.</p>
      ) : (
        <ol className={styles.timeline}>
          {historical.map((activity) => (
            <li
              key={`history-${activity.activityId}`}
              className={styles.timelineItem}
              aria-label={`${readable(activity.name)}, ${activity.actor}`}
              aria-level={1}
              style={{ "--activity-depth": 1 } as CSSProperties}
            >
              <button
                type="button"
                className={styles.timelineActivity}
                aria-current={selectedActivityId === activity.activityId ? "true" : undefined}
                onClick={() => onSelectActivity(activity.activityId)}
              >
                {activity.actor === "subagent"
                  ? <Bot aria-hidden="true" size={16} />
                  : <Wrench aria-hidden="true" size={16} />}
                <span><strong>{readable(activity.name)}</strong><small>{activity.actor} · {activity.status}</small></span>
              </button>
            </li>
          ))}
          {state.timeline.map((item, index) => {
            if (item.kind === "activity") {
              const activity = state.activities[item.activityId];
              if (activity === undefined) return null;
              const depth = activityDepth(activity, state.activities);
              const Icon = activity.actor === "subagent" ? Bot : Wrench;
              return (
                <li
                  key={`activity-${item.activityId}`}
                  className={styles.timelineItem}
                  aria-label={`${readable(activity.name)}, ${activity.actor}`}
                  aria-level={depth}
                  style={{ "--activity-depth": depth } as CSSProperties}
                >
                  <button
                    type="button"
                    className={styles.timelineActivity}
                    aria-current={selectedActivityId === activity.activityId ? "true" : undefined}
                    onClick={() => onSelectActivity(activity.activityId)}
                  >
                    <Icon aria-hidden="true" size={16} />
                    <span><strong>{readable(activity.name)}</strong><small>{activity.actor} · {activity.status}</small></span>
                  </button>
                </li>
              );
            }
            if (item.kind === "assistant") {
              return (
                <li key={`assistant-${index}`} className={styles.timelineItem}>
                  <MessageSquareText aria-hidden="true" size={16} />
                  <span><strong>Model</strong><small>{item.text}</small></span>
                </li>
              );
            }
            if (item.kind === "permission") {
              const decision = item.resolution?.answer ?? "Pending";
              return (
                <li key={`permission-${item.request.requestId}`} className={styles.timelineItem}>
                  <ShieldAlert aria-hidden="true" size={16} />
                  <span>
                    <strong>Permission · {readable(item.request.action)}</strong>
                    <small>{compactEvidence(item.request.reason)} · {decision}</small>
                    <details className={styles.timelineDetails}>
                      <summary>Permission details</summary>
                      <dl>
                        <div><dt>Reason</dt><dd>{item.request.reason}</dd></div>
                        <div><dt>Scope</dt><dd>{item.request.scope}</dd></div>
                        <div><dt>Decision</dt><dd>{decision}</dd></div>
                      </dl>
                    </details>
                  </span>
                </li>
              );
            }
            if (item.kind === "plan_review") {
              const decision = item.resolution === null ? "Pending" : item.resolution.approved ? "Approved" : "Revision requested";
              const detail = [compactEvidence(item.request.plan), item.resolution?.feedback ? compactEvidence(item.resolution.feedback) : null]
                .filter((value) => value !== null)
                .join(" · ");
              return (
                <li key={`plan-${item.request.requestId}`} className={styles.timelineItem}>
                  <ListChecks aria-hidden="true" size={16} />
                  <span>
                    <strong>Plan review · {decision}</strong>
                    <small>{detail}</small>
                    <details className={styles.timelineDetails}>
                      <summary>Plan review details</summary>
                      <dl>
                        <div><dt>Plan</dt><dd>{item.request.plan}</dd></div>
                        <div><dt>Decision</dt><dd>{decision}</dd></div>
                        <div><dt>Revision feedback</dt><dd>{item.resolution?.feedback || "None"}</dd></div>
                      </dl>
                    </details>
                  </span>
                </li>
              );
            }
            if (item.kind === "context") {
              const mode = typeof item.context.mode === "string" ? sentenceCase(item.context.mode) : "Unknown";
              return (
                <li key={`context-${item.turnId ?? "none"}-${item.sequence}`} className={styles.timelineItem}>
                  <GitMerge aria-hidden="true" size={16} />
                  <span><strong>Context management · {mode}</strong><small>Sequence {item.sequence}</small></span>
                </li>
              );
            }
            const isError = item.boundary === "error" || item.boundary === "activity_error";
            const Icon = isError ? CircleAlert : CheckCircle2;
            return (
              <li key={`boundary-${index}-${item.boundary}`} className={styles.timelineItem} data-error={isError || undefined}>
                <Icon aria-hidden="true" size={16} />
                <span><strong>{boundaryLabel(item.boundary)}</strong></span>
              </li>
            );
          })}
        </ol>
      )}
    </section>
  );
}
