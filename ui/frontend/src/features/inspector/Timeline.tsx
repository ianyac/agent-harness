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

import type { ActivityItem, TranscriptState, TranscriptTimelineItem } from "../../protocol/types";
import styles from "./inspector.module.css";

function readable(value: string): string {
  return value.replaceAll("_", " ");
}

function sentenceCase(value: string): string {
  const readableValue = readable(value);
  return `${readableValue[0]?.toLocaleUpperCase() ?? ""}${readableValue.slice(1)}`;
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
  if (boundary === "subagent_completion") return "Subagent complete";
  if (boundary === "permission") return "Permission boundary";
  return "Plan review boundary";
}

type TimelineProps = {
  readonly state: TranscriptState;
  readonly selectedActivityId: string | null;
  readonly onSelectActivity: (activityId: string) => void;
};

export function Timeline({ state, selectedActivityId, onSelectActivity }: TimelineProps) {
  return (
    <section className={styles.timelineSection} aria-labelledby="inspector-timeline-title">
      <h3 id="inspector-timeline-title">Timeline</h3>
      {state.timeline.length === 0 ? (
        <p className={styles.empty}>No activity recorded for this turn.</p>
      ) : (
        <ol className={styles.timeline}>
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
                  <span><strong>Permission · {readable(item.request.action)}</strong><small>{item.request.reason} · {decision}</small></span>
                </li>
              );
            }
            if (item.kind === "plan_review") {
              const decision = item.resolution === null ? "Pending" : item.resolution.approved ? "Approved" : "Revision requested";
              const detail = [item.request.plan, item.resolution?.feedback || null]
                .filter((value) => value !== null)
                .join(" · ");
              return (
                <li key={`plan-${item.request.requestId}`} className={styles.timelineItem}>
                  <ListChecks aria-hidden="true" size={16} />
                  <span><strong>Plan review · {decision}</strong><small>{detail}</small></span>
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
