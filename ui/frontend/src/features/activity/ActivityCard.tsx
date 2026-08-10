import { CheckCircle2, CircleAlert, Clock3 } from "lucide-react";
import { useEffect, useState } from "react";

import type { ActivityItem, JsonValue } from "../../protocol/types";
import styles from "../conversation/conversation.module.css";

type ActivityCardProps = {
  readonly activities: readonly ActivityItem[];
  readonly openInspector: (activityId: string) => void;
};

function formatDuration(durationMs: number): string {
  if (durationMs < 1_000) return `${durationMs}ms`;
  if (durationMs < 60_000) {
    const seconds = durationMs / 1_000;
    return `${Number.isInteger(seconds) ? seconds.toFixed(0) : seconds.toFixed(1)}s`;
  }
  const totalSeconds = Math.round(durationMs / 1_000);
  return `${Math.floor(totalSeconds / 60)}m ${totalSeconds % 60}s`;
}

// Wall-clock duration when every timed member has a parseable start; the
// summed fallback covers members whose start was never recorded. Historical
// activities may carry startedAt === "" and no durationMs — they contribute
// nothing, and a group with no timing data at all reports null.
function groupDurationMs(activities: readonly ActivityItem[], now: number): number | null {
  let sum = 0;
  let hasTiming = false;
  let spansComplete = true;
  let earliestStart = Number.POSITIVE_INFINITY;
  let latestEnd = Number.NEGATIVE_INFINITY;
  for (const activity of activities) {
    const started = Date.parse(activity.startedAt);
    let duration: number;
    if (activity.durationMs !== undefined) {
      duration = activity.durationMs;
    } else if (activity.status === "running" && Number.isFinite(started)) {
      duration = Math.max(0, now - started);
    } else {
      continue;
    }
    hasTiming = true;
    sum += duration;
    if (Number.isFinite(started)) {
      earliestStart = Math.min(earliestStart, started);
      latestEnd = Math.max(latestEnd, started + duration);
    } else {
      spansComplete = false;
    }
  }
  if (!hasTiming) return null;
  if (spansComplete) return latestEnd - earliestStart;
  return sum;
}

const previewLimit = 500;

function serializeResult(result: JsonValue): string {
  if (typeof result === "string") return result;
  if (result === null) return "";
  return JSON.stringify(result, null, 2);
}

function testSummary(activities: readonly ActivityItem[]): string | null {
  const summaries = activities
    .flatMap((activity) => {
      if (activity.result === undefined) return [];
      return serializeResult(activity.result).match(/\b\d+\s+(?:tests?\s+)?(?:passed|failed|skipped)\b/gi) ?? [];
    });
  return summaries.length === 0 ? null : [...new Set(summaries)].join(", ");
}

function readableName(name: string): string {
  return name.replaceAll("_", " ");
}

export function ActivityCard({ activities, openInspector }: ActivityCardProps) {
  const hasRunningActivity = activities.some((activity) => activity.status === "running");
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    if (!hasRunningActivity) return;
    setNow(Date.now());
    const timer = window.setInterval(() => setNow(Date.now()), 1_000);
    return () => window.clearInterval(timer);
  }, [hasRunningActivity]);

  const first = activities[0];
  if (first === undefined) return null;

  const hasError = activities.some((activity) => activity.status === "error" || activity.isError);
  const isRunning = !hasError && activities.some((activity) => activity.status === "running");
  const status = hasError ? "error" : isRunning ? "running" : "complete";
  const statusLabel = status === "error" ? "Failed" : status === "running" ? "Working" : "Complete";
  const Icon = status === "error" ? CircleAlert : status === "running" ? Clock3 : CheckCircle2;
  const durationMs = groupDurationMs(activities, now);
  const tests = testSummary(activities);
  const previewActivity = [...activities].reverse().find((activity) => activity.result !== undefined);
  const previewSource = previewActivity?.result === undefined ? "" : serializeResult(previewActivity.result);
  const preview = previewSource.length > previewLimit ? previewSource.slice(0, previewLimit) : previewSource;
  const actionLabel = `${activities.length} ${activities.length === 1 ? "action" : "actions"}`;
  const title = activities.length === 1
    ? readableName(first.name)
    : `${readableName(first.name)} and ${activities.length - 1} more`;
  const accessibleSummary = [
    statusLabel,
    durationMs === null ? null : formatDuration(durationMs),
    actionLabel,
    tests,
  ].filter((part) => part !== null).join(", ");

  return (
    <button
      type="button"
      className={styles.activityCard}
      data-status={status}
      aria-label={`Open activity: ${title}. ${accessibleSummary}`}
      onClick={() => openInspector(first.activityId)}
    >
      <span className={styles.activityHeading}>
        <Icon aria-hidden="true" size={17} />
        <span className={styles.activityTitle}>{title}</span>
      </span>
      <span className={styles.activityMeta}>
        <span>{statusLabel}</span>
        {durationMs === null ? null : <span>{formatDuration(durationMs)}</span>}
        <span>{actionLabel}</span>
        {tests === null ? null : <span>{tests}</span>}
      </span>
      {preview === "" ? null : <span className={styles.preview}>{preview}</span>}
    </button>
  );
}
