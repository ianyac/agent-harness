import type {
  ActivityCompleted,
  ActivityItem,
  ActivityStarted,
  HarnessMessage,
  ServerEvent,
  TranscriptState,
  TranscriptTimelineItem,
} from "./types";

export function emptyTranscript(): TranscriptState {
  return {
    generation: 1,
    lastSequence: 0,
    messages: [],
    streamingText: "",
    activities: {},
    activityOrder: [],
    timeline: [],
    permission: null,
    planReview: null,
    running: false,
    stopping: false,
    queued: null,
    safety: null,
    error: null,
  };
}

function startedActivity(event: ActivityStarted): ActivityItem {
  return {
    activityId: event.activity_id,
    turnId: event.turn_id,
    parentActivityId: event.parent_activity_id,
    actor: event.actor,
    name: event.name,
    args: event.args,
    startedAt: event.started_at,
    status: "running",
  };
}

function completedActivity(event: ActivityCompleted): ActivityItem {
  return {
    activityId: event.activity_id,
    turnId: event.turn_id,
    parentActivityId: event.parent_activity_id,
    actor: event.actor,
    name: event.name,
    args: event.args,
    startedAt: event.started_at,
    status: event.is_error ? "error" : "complete",
    result: event.result,
    isError: event.is_error,
    durationMs: event.duration_ms,
  };
}

function appendActivity(
  state: TranscriptState,
  item: ActivityItem,
): Pick<TranscriptState, "activities" | "activityOrder" | "timeline"> {
  const known = Object.prototype.hasOwnProperty.call(state.activities, item.activityId);
  return {
    activities: { ...state.activities, [item.activityId]: item },
    activityOrder: known ? state.activityOrder : [...state.activityOrder, item.activityId],
    timeline: known
      ? state.timeline
      : [...state.timeline, { kind: "activity", activityId: item.activityId }],
  };
}

function appendAssistantDelta(
  timeline: readonly TranscriptTimelineItem[],
  text: string,
): TranscriptTimelineItem[] {
  const last = timeline[timeline.length - 1];
  if (last?.kind === "assistant" && last.messageIndex === null) {
    return [
      ...timeline.slice(0, -1),
      { ...last, text: last.text + text },
    ];
  }
  return [...timeline, { kind: "assistant", text, messageIndex: null }];
}

function resetActiveAssistant(timeline: readonly TranscriptTimelineItem[]): TranscriptTimelineItem[] {
  const last = timeline[timeline.length - 1];
  return last?.kind === "assistant" && last.messageIndex === null
    ? timeline.slice(0, -1)
    : [...timeline];
}

function streamedText(timeline: readonly TranscriptTimelineItem[]): string {
  return timeline.flatMap((item) => item.kind === "assistant" ? [item.text] : []).join("");
}

function messageText(message: HarnessMessage): string | null {
  if (message.role !== "assistant") return null;
  if (typeof message.content === "string") return message.content === "" ? null : message.content;
  if (!Array.isArray(message.content)) return null;
  const parts = message.content.flatMap((part) => {
    if (typeof part === "string") return [part];
    if (part === null || Array.isArray(part) || typeof part !== "object") return [];
    return typeof part.text === "string" ? [part.text] : [];
  });
  return parts.length === 0 ? null : parts.join("\n");
}

function reconcileAssistantTimeline(
  timeline: readonly TranscriptTimelineItem[],
  messages: readonly HarnessMessage[],
  finalText: string,
): TranscriptTimelineItem[] {
  let latestUserIndex = -1;
  messages.forEach((message, messageIndex) => {
    if (message.role === "user") latestUserIndex = messageIndex;
  });
  const candidates = messages.flatMap((message, messageIndex) => {
    if (messageIndex <= latestUserIndex) return [];
    const text = messageText(message);
    return text === null ? [] : [{ messageIndex, text }];
  });
  const used = new Set<number>();
  let candidateCursor = 0;
  const reconciled = timeline.map((item): TranscriptTimelineItem => {
    if (item.kind !== "assistant") return item;
    const matchOffset = candidates.slice(candidateCursor).findIndex(
      (candidate) => candidate.text === item.text,
    );
    if (matchOffset === -1) return item;
    const candidatePosition = candidateCursor + matchOffset;
    const candidate = candidates[candidatePosition];
    candidateCursor = candidatePosition + 1;
    used.add(candidate.messageIndex);
    return { ...item, text: candidate.text, messageIndex: candidate.messageIndex };
  });

  const finalCandidate = [...candidates].reverse().find(
    (candidate) => candidate.text === finalText,
  ) ?? candidates[candidates.length - 1];
  if (finalCandidate === undefined || used.has(finalCandidate.messageIndex)) return reconciled;

  for (let index = reconciled.length - 1; index >= 0; index -= 1) {
    const item = reconciled[index];
    if (item.kind === "assistant" && item.messageIndex === null) {
      reconciled[index] = {
        ...item,
        text: finalCandidate.text,
        messageIndex: finalCandidate.messageIndex,
      };
      return reconciled;
    }
  }
  return [
    ...reconciled,
    { kind: "assistant", text: finalCandidate.text, messageIndex: finalCandidate.messageIndex },
  ];
}

function boundary(
  timeline: readonly TranscriptTimelineItem[],
  value: "permission" | "plan_review" | "error" | "turn_completion",
): TranscriptTimelineItem[] {
  return [...timeline, { kind: "boundary", boundary: value }];
}

function discardUncommittedAssistants(
  timeline: readonly TranscriptTimelineItem[],
): TranscriptTimelineItem[] {
  return timeline.filter((item) => item.kind !== "assistant" || item.messageIndex !== null);
}

function terminalState(state: TranscriptState): TranscriptState {
  return {
    ...state,
    streamingText: "",
    permission: null,
    planReview: null,
    running: false,
    stopping: false,
    queued: null,
  };
}

export function transcriptReducer(state: TranscriptState, event: ServerEvent): TranscriptState {
  if (event.type === "session_snapshot") {
    const sameGeneration = event.generation === state.generation;
    if (event.generation < state.generation || (sameGeneration && event.sequence <= state.lastSequence)) {
      return state;
    }
    return {
      generation: event.generation,
      lastSequence: event.sequence,
      messages: event.messages,
      streamingText: "",
      activities: {},
      activityOrder: [],
      timeline: [],
      permission: null,
      planReview: null,
      running: event.running,
      stopping: false,
      queued: event.queued_message,
      safety: event.safety,
      error: null,
    };
  }

  if (event.generation !== state.generation || event.sequence <= state.lastSequence) {
    return state;
  }

  const accepted = { ...state, lastSequence: event.sequence };
  switch (event.type) {
    case "turn_started":
      return {
        ...accepted,
        streamingText: "",
        activities: {},
        activityOrder: [],
        timeline: [],
        permission: null,
        planReview: null,
        running: true,
        stopping: false,
        queued: null,
        error: null,
      };
    case "assistant_delta": {
      const timeline = appendAssistantDelta(state.timeline, event.text);
      return { ...accepted, timeline, streamingText: streamedText(timeline) };
    }
    case "stream_reset": {
      const timeline = resetActiveAssistant(state.timeline);
      return { ...accepted, timeline, streamingText: streamedText(timeline) };
    }
    case "activity_started":
      return { ...accepted, ...appendActivity(state, startedActivity(event)) };
    case "activity_completed":
      return { ...accepted, ...appendActivity(state, completedActivity(event)) };
    case "permission_requested":
      return {
        ...accepted,
        timeline: boundary(state.timeline, "permission"),
        permission: {
          turnId: event.turn_id,
          requestId: event.request_id,
          action: event.action,
          scope: event.scope,
          reason: event.reason,
        },
      };
    case "permission_resolved":
      return {
        ...accepted,
        permission: state.permission?.requestId === event.request_id ? null : state.permission,
      };
    case "plan_approval_requested":
      return {
        ...accepted,
        timeline: boundary(state.timeline, "plan_review"),
        planReview: { turnId: event.turn_id, requestId: event.request_id, plan: event.plan },
      };
    case "plan_approval_resolved":
      return {
        ...accepted,
        planReview: state.planReview?.requestId === event.request_id ? null : state.planReview,
      };
    case "context_updated":
      return accepted;
    case "turn_stopping":
      return { ...accepted, running: true, stopping: true };
    case "turn_completed": {
      const reconciled = reconcileAssistantTimeline(state.timeline, event.messages, event.final_text);
      return {
        ...terminalState(accepted),
        messages: event.messages,
        timeline: boundary(reconciled, "turn_completion"),
        error: null,
      };
    }
    case "turn_cancelled":
      return {
        ...terminalState(accepted),
        timeline: boundary(discardUncommittedAssistants(state.timeline), "turn_completion"),
        error: null,
      };
    case "turn_failed":
      return {
        ...terminalState(accepted),
        timeline: boundary(discardUncommittedAssistants(state.timeline), "error"),
        error: { category: event.error_category, message: event.message },
      };
    case "safety_updated":
      return { ...accepted, safety: event.safety };
  }
}
