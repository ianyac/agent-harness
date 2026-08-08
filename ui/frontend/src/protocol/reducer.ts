import type {
  ActivityCompleted,
  ActivityItem,
  ActivityStarted,
  ServerEvent,
  TranscriptState,
} from "./types";

export function emptyTranscript(): TranscriptState {
  return {
    generation: 1,
    lastSequence: 0,
    messages: [],
    streamingText: "",
    activities: {},
    activityOrder: [],
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

function appendActivity(state: TranscriptState, item: ActivityItem): Pick<TranscriptState, "activities" | "activityOrder"> {
  return {
    activities: { ...state.activities, [item.activityId]: item },
    activityOrder: Object.prototype.hasOwnProperty.call(state.activities, item.activityId)
      ? state.activityOrder
      : [...state.activityOrder, item.activityId],
  };
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
        permission: null,
        planReview: null,
        running: true,
        stopping: false,
        queued: null,
        error: null,
      };
    case "assistant_delta":
      return { ...accepted, streamingText: state.streamingText + event.text };
    case "stream_reset":
      return { ...accepted, streamingText: "" };
    case "activity_started":
      return { ...accepted, ...appendActivity(state, startedActivity(event)) };
    case "activity_completed":
      return { ...accepted, ...appendActivity(state, completedActivity(event)) };
    case "permission_requested":
      return {
        ...accepted,
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
    case "turn_completed":
      return { ...terminalState(accepted), messages: event.messages, error: null };
    case "turn_cancelled":
      return { ...terminalState(accepted), error: null };
    case "turn_failed":
      return {
        ...terminalState(accepted),
        error: { category: event.error_category, message: event.message },
      };
    case "safety_updated":
      return { ...accepted, safety: event.safety };
  }
}
