import type { JsonObject, JsonValue, QueuedMessage, ServerEvent } from "./types";

export type ParseResult<T> = { ok: true; value: T } | { ok: false; error: string };

type Validator = (value: unknown) => boolean;

const maxJsonDepth = 64;

function isPlainObject(value: unknown): value is Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    return false;
  }
  const prototype = Object.getPrototypeOf(value);
  return prototype === Object.prototype || prototype === null;
}

function hasUnicodeScalars(value: string): boolean {
  for (let index = 0; index < value.length; index += 1) {
    const unit = value.charCodeAt(index);
    if (unit >= 0xd800 && unit <= 0xdbff) {
      if (index + 1 >= value.length) return false;
      const next = value.charCodeAt(index + 1);
      if (next < 0xdc00 || next > 0xdfff) return false;
      index += 1;
    } else if (unit >= 0xdc00 && unit <= 0xdfff) {
      return false;
    }
  }
  return true;
}

const isString: Validator = (value): value is string =>
  typeof value === "string" && hasUnicodeScalars(value);
const isBoolean: Validator = (value): value is boolean => typeof value === "boolean";
const isPositiveInteger: Validator = (value): value is number =>
  typeof value === "number" && Number.isSafeInteger(value) && value >= 1;
const isNonNegativeInteger: Validator = (value): value is number =>
  typeof value === "number" && Number.isSafeInteger(value) && value >= 0;
const isIdentifier: Validator = (value): value is string =>
  isString(value) && (value as string).trim().length > 0;
const isNullableIdentifier: Validator = (value) => value === null || isIdentifier(value);
const isTurnMode: Validator = (value) => value === "base" || value === "plan";
const isPermissionDecision: Validator = (value) =>
  value === "yes" || value === "no" || value === "always";

function isJsonValue(value: unknown, depth = 0, seen = new Set<object>()): value is JsonValue {
  if (value === null || typeof value === "boolean") return true;
  if (typeof value === "string") return hasUnicodeScalars(value);
  if (typeof value === "number") return Number.isFinite(value);
  if (depth >= maxJsonDepth || typeof value !== "object") return false;
  if (seen.has(value)) return false;
  seen.add(value);

  if (Array.isArray(value)) {
    const valid = value.every((item) => isJsonValue(item, depth + 1, seen));
    seen.delete(value);
    return valid;
  }
  if (!isPlainObject(value)) return false;
  const valid = Object.entries(value).every(
    ([key, item]) => hasUnicodeScalars(key) && isJsonValue(item, depth + 1, seen),
  );
  seen.delete(value);
  return valid;
}

const isJsonObject: Validator = (value): value is JsonObject =>
  isPlainObject(value) && isJsonValue(value);

const isMessageList: Validator = (value) =>
  Array.isArray(value) && value.every((message) => isJsonObject(message));

function isQueuedMessage(value: unknown): value is QueuedMessage {
  return matchesShape(value, {
    type: (item) => item === "queue_message",
    text: (item) => isString(item) && (item as string).trim().length > 0,
    mode: isTurnMode,
  });
}

const isNullableQueuedMessage: Validator = (value) => value === null || isQueuedMessage(value);

function matchesShape(value: unknown, shape: Record<string, Validator>): boolean {
  if (!isPlainObject(value)) return false;
  const expected = Object.keys(shape);
  const actual = Object.keys(value);
  if (actual.length !== expected.length || actual.some((key) => !(key in shape))) return false;
  return expected.every((key) => Object.prototype.hasOwnProperty.call(value, key) && shape[key](value[key]));
}

const envelopeShape = {
  type: isString,
  session_id: isIdentifier,
  generation: isPositiveInteger,
  sequence: isPositiveInteger,
  turn_id: isNullableIdentifier,
};

const shapes: Record<ServerEvent["type"], Record<string, Validator>> = {
  session_snapshot: {
    ...envelopeShape,
    type: (value) => value === "session_snapshot",
    messages: isMessageList,
    running: isBoolean,
    queued_message: isNullableQueuedMessage,
    safety: isJsonObject,
  },
  turn_started: {
    ...envelopeShape,
    type: (value) => value === "turn_started",
    turn_id: isIdentifier,
    mode: isTurnMode,
  },
  assistant_delta: {
    ...envelopeShape,
    type: (value) => value === "assistant_delta",
    turn_id: isIdentifier,
    text: isString,
  },
  stream_reset: {
    ...envelopeShape,
    type: (value) => value === "stream_reset",
    turn_id: isIdentifier,
  },
  activity_started: {
    ...envelopeShape,
    type: (value) => value === "activity_started",
    turn_id: isIdentifier,
    activity_id: isIdentifier,
    parent_activity_id: isNullableIdentifier,
    actor: isIdentifier,
    name: isIdentifier,
    args: isJsonObject,
    started_at: isString,
  },
  activity_completed: {
    ...envelopeShape,
    type: (value) => value === "activity_completed",
    turn_id: isIdentifier,
    activity_id: isIdentifier,
    parent_activity_id: isNullableIdentifier,
    actor: isIdentifier,
    name: isIdentifier,
    args: isJsonObject,
    result: isJsonValue,
    is_error: isBoolean,
    started_at: isString,
    duration_ms: isNonNegativeInteger,
  },
  permission_requested: {
    ...envelopeShape,
    type: (value) => value === "permission_requested",
    turn_id: isIdentifier,
    request_id: isIdentifier,
    action: isIdentifier,
    scope: isString,
    reason: isString,
  },
  permission_resolved: {
    ...envelopeShape,
    type: (value) => value === "permission_resolved",
    turn_id: isIdentifier,
    request_id: isIdentifier,
    answer: isPermissionDecision,
  },
  plan_approval_requested: {
    ...envelopeShape,
    type: (value) => value === "plan_approval_requested",
    turn_id: isIdentifier,
    request_id: isIdentifier,
    plan: isString,
  },
  plan_approval_resolved: {
    ...envelopeShape,
    type: (value) => value === "plan_approval_resolved",
    turn_id: isIdentifier,
    request_id: isIdentifier,
    approved: isBoolean,
    feedback: isString,
  },
  context_updated: {
    ...envelopeShape,
    type: (value) => value === "context_updated",
    context: isJsonObject,
  },
  turn_stopping: {
    ...envelopeShape,
    type: (value) => value === "turn_stopping",
    turn_id: isIdentifier,
  },
  turn_completed: {
    ...envelopeShape,
    type: (value) => value === "turn_completed",
    turn_id: isIdentifier,
    messages: isMessageList,
    final_text: isString,
  },
  turn_cancelled: {
    ...envelopeShape,
    type: (value) => value === "turn_cancelled",
    turn_id: isIdentifier,
  },
  turn_failed: {
    ...envelopeShape,
    type: (value) => value === "turn_failed",
    turn_id: isIdentifier,
    error_category: isIdentifier,
    message: isString,
  },
  safety_updated: {
    ...envelopeShape,
    type: (value) => value === "safety_updated",
    safety: isJsonObject,
  },
};

export function parseServerEvent(input: unknown): ParseResult<ServerEvent> {
  try {
    if (!isPlainObject(input) || typeof input.type !== "string") {
      return { ok: false, error: "Invalid server event envelope" };
    }
    const shape = shapes[input.type as ServerEvent["type"]];
    if (shape === undefined || !matchesShape(input, shape)) {
      return { ok: false, error: "Invalid server event payload" };
    }
    return { ok: true, value: input as ServerEvent };
  } catch {
    return { ok: false, error: "Invalid server event payload" };
  }
}
