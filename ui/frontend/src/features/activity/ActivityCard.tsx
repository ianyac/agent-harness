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
  const seconds = durationMs / 1_000;
  return `${Number.isInteger(seconds) ? seconds.toFixed(0) : seconds.toFixed(1)}s`;
}

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
  const showsDuration = activities.some(
    (activity) => activity.durationMs !== undefined || activity.status === "running",
  );
  const durationMs = activities.reduce((total, activity) => {
    if (activity.durationMs !== undefined) return total + activity.durationMs;
    if (activity.status !== "running") return total;
    const started = Date.parse(activity.startedAt);
    return total + (Number.isFinite(started) ? Math.max(0, now - started) : 0);
  }, 0);
  const tests = testSummary(activities);
  const previewActivity = [...activities].reverse().find((activity) => activity.result !== undefined);
  const preview = previewActivity?.result === undefined ? "" : serializeResult(previewActivity.result);
  const actionLabel = `${activities.length} ${activities.length === 1 ? "action" : "actions"}`;
  const title = activities.length === 1
    ? readableName(first.name)
    : `${readableName(first.name)} and ${activities.length - 1} more`;
  const accessibleSummary = [
    statusLabel,
    showsDuration ? formatDuration(durationMs) : null,
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
        {showsDuration ? <span>{formatDuration(durationMs)}</span> : null}
        <span>{actionLabel}</span>
        {tests === null ? null : <span>{tests}</span>}
      </span>
      {preview === "" ? null : <span className={styles.preview}>{preview}</span>}
    </button>
  );
}
