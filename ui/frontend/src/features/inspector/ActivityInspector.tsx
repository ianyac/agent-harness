import * as Dialog from "@radix-ui/react-dialog";
import { Copy, PanelRightClose, Pin, PinOff } from "lucide-react";
import { useEffect, useLayoutEffect, useRef, useState } from "react";
import type { CSSProperties, KeyboardEvent, PointerEvent as ReactPointerEvent } from "react";

import type { ActivityItem, JsonValue, TranscriptState } from "../../protocol/types";
import { copyToClipboard } from "../conversation/CodeBlock";
import type { CopyText } from "../conversation/CodeBlock";
import { ContextSummary } from "./ContextSummary";
import { SafetySummary } from "./SafetySummary";
import { Timeline } from "./Timeline";
import styles from "./inspector.module.css";
import { clampInspectorWidth, INSPECTOR_MAX_WIDTH, INSPECTOR_MIN_WIDTH } from "./useInspector";

function sortedValue(value: JsonValue): JsonValue {
  if (Array.isArray(value)) return value.map(sortedValue);
  if (value === null || typeof value !== "object") return value;
  return Object.fromEntries(
    Object.keys(value).sort((a, b) => a.localeCompare(b)).map((key) => [key, sortedValue(value[key])]),
  );
}

export function serializeInspectorValue(value: JsonValue): string {
  if (typeof value === "string") return value;
  return JSON.stringify(sortedValue(value), null, 2);
}

function duration(activity: ActivityItem): string {
  if (activity.durationMs === undefined) return "Unknown";
  if (activity.durationMs < 1_000) return `${activity.durationMs}ms`;
  const seconds = activity.durationMs / 1_000;
  return `${Number.isInteger(seconds) ? seconds.toFixed(0) : seconds.toFixed(1)}s`;
}

function statusLabel(status: ActivityItem["status"]): string {
  return `${status[0].toLocaleUpperCase()}${status.slice(1)}`;
}

type PayloadProps = {
  readonly label: string;
  readonly value: JsonValue;
  readonly onCopy: (value: string) => void;
};

function Payload({ label, value, onCopy }: PayloadProps) {
  const serialized = serializeInspectorValue(value);
  return (
    <details open className={styles.payloadDetails}>
      <summary>{label}</summary>
      <button type="button" className={styles.copyButton} aria-label={`Copy ${label.toLocaleLowerCase()}`} onClick={() => onCopy(serialized)}>
        <Copy aria-hidden="true" size={14} /> Copy
      </button>
      <pre>{serialized}</pre>
    </details>
  );
}

function SelectedActivity({
  activity,
  onCopy,
}: {
  readonly activity: ActivityItem;
  readonly onCopy: (value: string) => void;
}) {
  return (
    <section className={styles.detail} aria-label="Selected activity detail">
      <div className={styles.detailHeading}>
        <div><span className={styles.eyebrow}>{activity.actor}</span><h3>{activity.name.replaceAll("_", " ")}</h3></div>
        <span className={styles.status} data-error={activity.status === "error" || undefined}>{statusLabel(activity.status)}</span>
      </div>
      <dl className={styles.detailMeta}>
        <div><dt>Duration</dt><dd>{duration(activity)}</dd></div>
        <div><dt>Error</dt><dd>{activity.isError === undefined ? "Unknown" : activity.isError ? "Yes" : "No"}</dd></div>
      </dl>
      <Payload label="Arguments" value={activity.args} onCopy={onCopy} />
      {activity.result === undefined ? null : (
        <Payload label="Result" value={activity.result} onCopy={onCopy} />
      )}
    </section>
  );
}

export type ActivityInspectorProps = {
  readonly open: boolean;
  readonly narrow: boolean;
  readonly width: number;
  readonly pinned: boolean;
  readonly sessionId: string;
  readonly contextMode: string;
  readonly state: TranscriptState;
  readonly selectedActivityId: string | null;
  readonly onClose: () => void;
  readonly onPinnedChange: (pinned: boolean) => void;
  readonly onSelectActivity: (activityId: string) => void;
  readonly onWidthChange: (width: number) => void;
  readonly copyText?: CopyText;
};

export function ActivityInspector({
  open,
  narrow,
  width,
  pinned,
  sessionId,
  contextMode,
  state,
  selectedActivityId,
  onClose,
  onPinnedChange,
  onSelectActivity,
  onWidthChange,
  copyText = copyToClipboard,
}: ActivityInspectorProps) {
  const closeRef = useRef<HTMLButtonElement>(null);
  const stopPointerResize = useRef<(() => void) | null>(null);
  const nextCopyOperation = useRef(0);
  const currentCopyOperation = useRef<{
    readonly id: number;
    readonly sessionId: string;
    readonly selectedActivityId: string | null;
  } | null>(null);
  const currentView = useRef({ sessionId, selectedActivityId });
  const [copyFeedback, setCopyFeedback] = useState<{
    readonly id: number;
    readonly status: "copied" | "error";
  } | null>(null);
  const selected = selectedActivityId === null ? undefined : state.activities[selectedActivityId];
  const clampedWidth = clampInspectorWidth(width);
  currentView.current = { sessionId, selectedActivityId };

  useEffect(() => () => stopPointerResize.current?.(), []);
  useLayoutEffect(() => {
    currentCopyOperation.current = null;
    setCopyFeedback(null);
  }, [sessionId, selectedActivityId]);

  const copyValue = async (value: string) => {
    const operation = {
      id: ++nextCopyOperation.current,
      sessionId,
      selectedActivityId,
    };
    currentCopyOperation.current = operation;
    setCopyFeedback(null);
    let status: "copied" | "error" = "copied";
    try {
      await copyText(value);
    } catch {
      status = "error";
    }
    const view = currentView.current;
    if (
      currentCopyOperation.current !== operation
      || view.sessionId !== operation.sessionId
      || view.selectedActivityId !== operation.selectedActivityId
    ) return;
    setCopyFeedback({ id: operation.id, status });
  };

  const onResizeKey = (event: KeyboardEvent<HTMLDivElement>) => {
    const next = event.key === "ArrowLeft" ? clampedWidth + 16
      : event.key === "ArrowRight" ? clampedWidth - 16
        : event.key === "Home" ? INSPECTOR_MIN_WIDTH
          : event.key === "End" ? INSPECTOR_MAX_WIDTH : null;
    if (next === null) return;
    event.preventDefault();
    onWidthChange(clampInspectorWidth(next));
  };

  const startPointerResize = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (event.button !== 0) return;
    event.preventDefault();
    stopPointerResize.current?.();
    const startX = event.clientX;
    const startWidth = clampedWidth;
    const onMove = (move: PointerEvent) => {
      onWidthChange(clampInspectorWidth(startWidth + startX - move.clientX));
    };
    const stop = () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", stop);
      window.removeEventListener("pointercancel", stop);
      if (stopPointerResize.current === stop) stopPointerResize.current = null;
    };
    stopPointerResize.current = stop;
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", stop);
    window.addEventListener("pointercancel", stop);
  };

  return (
    <Dialog.Root open={open} modal={narrow} onOpenChange={(next) => { if (!next) onClose(); }}>
      <Dialog.Portal>
        {narrow ? <Dialog.Overlay className={styles.overlay} data-testid="inspector-overlay" /> : null}
        <Dialog.Content
          className={styles.inspector}
          data-narrow={narrow || undefined}
          data-modal={String(narrow)}
          style={{ "--inspector-width": `${clampedWidth}px` } as CSSProperties}
          onOpenAutoFocus={(event) => {
            event.preventDefault();
            closeRef.current?.focus();
          }}
          onCloseAutoFocus={(event) => event.preventDefault()}
          onInteractOutside={(event) => event.preventDefault()}
        >
          {!narrow ? (
            <div
              className={styles.resizeHandle}
              role="separator"
              aria-label="Resize activity inspector"
              aria-orientation="vertical"
              aria-valuemin={INSPECTOR_MIN_WIDTH}
              aria-valuemax={INSPECTOR_MAX_WIDTH}
              aria-valuenow={clampedWidth}
              tabIndex={0}
              onKeyDown={onResizeKey}
              onPointerDown={startPointerResize}
            >
              <span aria-hidden="true" className={styles.resizeRule} data-resize-rule />
            </div>
          ) : null}
          <div className={styles.header}>
            <div>
              <Dialog.Title>Activity inspector</Dialog.Title>
              <Dialog.Description>
                Review this session’s chronological harness activity and authoritative safety context.
              </Dialog.Description>
            </div>
            <div className={styles.headerActions}>
              <button
                type="button"
                className={styles.iconButton}
                aria-label={pinned ? "Unpin inspector" : "Pin inspector"}
                aria-pressed={pinned}
                onClick={() => onPinnedChange(!pinned)}
              >
                {pinned ? <PinOff aria-hidden="true" size={17} /> : <Pin aria-hidden="true" size={17} />}
              </button>
              <Dialog.Close asChild>
                <button ref={closeRef} type="button" className={styles.iconButton} aria-label="Close activity inspector">
                  <PanelRightClose aria-hidden="true" size={18} />
                </button>
              </Dialog.Close>
            </div>
          </div>
          <div className={styles.content}>
            {selected === undefined ? (
              <div className={styles.overview}>
                <SafetySummary safety={state.safety} />
                <ContextSummary contextMode={contextMode} latestContext={state.latestContext} />
              </div>
            ) : (
              <SelectedActivity activity={selected} onCopy={(value) => void copyValue(value)} />
            )}
            <Timeline state={state} selectedActivityId={selectedActivityId} onSelectActivity={onSelectActivity} />
          </div>
          <span className={styles.srOnly} role="status" aria-label="Inspector copy status" aria-live="polite" aria-atomic="true">
            {copyFeedback === null ? null : (
              <span key={copyFeedback.id}>
                {copyFeedback.status === "copied" ? "Copied" : "Copy failed. Try again."}
              </span>
            )}
          </span>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
