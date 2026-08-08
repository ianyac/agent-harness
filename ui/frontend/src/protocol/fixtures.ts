import type { ServerEvent } from "./types";

type EventOf<T extends ServerEvent["type"]> = Extract<ServerEvent, { type: T }>;
type EventOverrides<T extends ServerEvent["type"]> = Partial<Omit<EventOf<T>, "type">>;

const envelope = {
  session_id: "s1",
  generation: 1,
  sequence: 1,
  turn_id: "t1",
};

const defaults: { [T in ServerEvent["type"]]: EventOf<T> } = {
  session_snapshot: {
    ...envelope,
    type: "session_snapshot",
    turn_id: null,
    messages: [],
    running: false,
    queued_message: null,
    safety: {},
  },
  turn_started: { ...envelope, type: "turn_started", mode: "base" },
  assistant_delta: { ...envelope, type: "assistant_delta", text: "" },
  stream_reset: { ...envelope, type: "stream_reset" },
  activity_started: {
    ...envelope,
    type: "activity_started",
    activity_id: "a1",
    parent_activity_id: null,
    actor: "tool",
    name: "read_file",
    args: {},
    started_at: "2026-08-08T00:00:00Z",
  },
  activity_completed: {
    ...envelope,
    type: "activity_completed",
    activity_id: "a1",
    parent_activity_id: null,
    actor: "tool",
    name: "read_file",
    args: {},
    result: null,
    is_error: false,
    started_at: "2026-08-08T00:00:00Z",
    duration_ms: 0,
  },
  permission_requested: {
    ...envelope,
    type: "permission_requested",
    request_id: "p1",
    action: "write_file",
    scope: "README.md",
    reason: "Needs an edit",
  },
  permission_resolved: {
    ...envelope,
    type: "permission_resolved",
    request_id: "p1",
    answer: "yes",
  },
  plan_approval_requested: {
    ...envelope,
    type: "plan_approval_requested",
    request_id: "r1",
    plan: "1. Make the change",
  },
  plan_approval_resolved: {
    ...envelope,
    type: "plan_approval_resolved",
    request_id: "r1",
    approved: true,
    feedback: "",
  },
  context_updated: { ...envelope, type: "context_updated", context: {} },
  turn_stopping: { ...envelope, type: "turn_stopping" },
  turn_completed: {
    ...envelope,
    type: "turn_completed",
    messages: [],
    final_text: "",
  },
  turn_cancelled: { ...envelope, type: "turn_cancelled" },
  turn_failed: {
    ...envelope,
    type: "turn_failed",
    error_category: "unknown",
    message: "Turn failed",
  },
  safety_updated: { ...envelope, type: "safety_updated", turn_id: null, safety: {} },
};

let nextSequence = 0;

export function event<T extends ServerEvent["type"]>(
  type: T,
  overrides: EventOverrides<T> = {},
): EventOf<T> {
  nextSequence += 1;
  return { ...defaults[type], sequence: nextSequence, ...overrides, type } as EventOf<T>;
}
