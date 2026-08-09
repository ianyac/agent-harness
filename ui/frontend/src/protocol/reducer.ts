import type {
  ActivityCompleted,
  ActivityItem,
  ActivityStarted,
  HarnessMessage,
  PermissionDecision,
  ServerEvent,
  TranscriptBoundary,
  TranscriptPlanReviewTimelineItem,
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
    latestContext: null,
    error: null,
    terminal: null,
    activeTurn: null,
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
  const text = parts.join("\n");
  return text.trim() === "" ? null : text;
}

function reconcileAssistantTimeline(
  timeline: readonly TranscriptTimelineItem[],
  messages: readonly HarnessMessage[],
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
  let candidateCursor = 0;
  const reconciled = timeline.flatMap((item): TranscriptTimelineItem[] => {
    if (item.kind !== "assistant") return [item];
    if (item.text === "") return [];
    const candidate = candidates[candidateCursor];
    if (candidate === undefined) return [];
    candidateCursor += 1;
    return [{ ...item, text: candidate.text, messageIndex: candidate.messageIndex }];
  });
  return [
    ...reconciled,
    ...candidates.slice(candidateCursor).map((candidate): TranscriptTimelineItem => ({
      kind: "assistant",
      text: candidate.text,
      messageIndex: candidate.messageIndex,
    })),
  ];
}

function boundary(
  timeline: readonly TranscriptTimelineItem[],
  value: TranscriptBoundary,
): TranscriptTimelineItem[] {
  return [...timeline, { kind: "boundary", boundary: value }];
}

function resolvePermission(
  timeline: readonly TranscriptTimelineItem[],
  requestId: string,
  answer: PermissionDecision,
): TranscriptTimelineItem[] {
  return timeline.map((item) =>
    item.kind === "permission" && item.request.requestId === requestId
      ? { ...item, resolution: { answer } }
      : item,
  );
}

function resolvePlanReview(
  timeline: readonly TranscriptTimelineItem[],
  requestId: string,
  resolution: NonNullable<TranscriptPlanReviewTimelineItem["resolution"]>,
): TranscriptTimelineItem[] {
  return timeline.map((item) =>
    item.kind === "plan_review" && item.request.requestId === requestId
      ? { ...item, resolution }
      : item,
  );
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
    activeTurn: null,
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
      latestContext: null,
      error: null,
      terminal: null,
      activeTurn: null,
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
        terminal: null,
        activeTurn: {
          generation: event.generation,
          sequence: event.sequence,
          turnId: event.turn_id,
        },
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
    case "activity_completed": {
      const activity = completedActivity(event);
      const appended = appendActivity(state, activity);
      const completionBoundary = activity.status === "error"
        ? "activity_error"
        : activity.actor === "subagent" ? "subagent_completion" : null;
      return {
        ...accepted,
        ...appended,
        timeline: completionBoundary === null
          ? appended.timeline
          : boundary(appended.timeline, completionBoundary),
      };
    }
    case "permission_requested":
      {
        const request = {
          turnId: event.turn_id,
          requestId: event.request_id,
          action: event.action,
          scope: event.scope,
          reason: event.reason,
        };
        return {
          ...accepted,
          timeline: [...state.timeline, { kind: "permission", request, resolution: null }],
          permission: request,
        };
      }
    case "permission_resolved":
      return {
        ...accepted,
        timeline: resolvePermission(state.timeline, event.request_id, event.answer),
        permission: state.permission?.requestId === event.request_id ? null : state.permission,
      };
    case "plan_approval_requested":
      {
        const request = {
          turnId: event.turn_id,
          requestId: event.request_id,
          plan: event.plan,
        };
        return {
          ...accepted,
          timeline: [...state.timeline, { kind: "plan_review", request, resolution: null }],
          planReview: request,
        };
      }
    case "plan_approval_resolved":
      return {
        ...accepted,
        timeline: resolvePlanReview(state.timeline, event.request_id, {
          approved: event.approved,
          feedback: event.feedback,
        }),
        planReview: state.planReview?.requestId === event.request_id ? null : state.planReview,
      };
    case "context_updated":
      return {
        ...accepted,
        latestContext: event.context,
        timeline: [
          ...state.timeline,
          {
            kind: "context",
            turnId: event.turn_id,
            sequence: event.sequence,
            context: event.context,
          },
        ],
      };
    case "turn_stopping":
      return { ...accepted, running: true, stopping: true };
    case "turn_completed": {
      const reconciled = reconcileAssistantTimeline(state.timeline, event.messages);
      return {
        ...terminalState(accepted),
        messages: event.messages,
        timeline: boundary(reconciled, "turn_completion"),
        error: null,
        terminal: {
          kind: "completed",
          generation: event.generation,
          sequence: event.sequence,
          turnId: event.turn_id,
        },
      };
    }
    case "turn_cancelled":
      return {
        ...terminalState(accepted),
        timeline: boundary(discardUncommittedAssistants(state.timeline), "turn_completion"),
        error: null,
        terminal: {
          kind: "cancelled",
          generation: event.generation,
          sequence: event.sequence,
          turnId: event.turn_id,
        },
      };
    case "turn_failed":
      return {
        ...terminalState(accepted),
        timeline: boundary(discardUncommittedAssistants(state.timeline), "error"),
        error: { category: event.error_category, message: event.message },
        terminal: {
          kind: "failed",
          generation: event.generation,
          sequence: event.sequence,
          turnId: event.turn_id,
        },
      };
    case "safety_updated":
      return { ...accepted, safety: event.safety };
  }
}
