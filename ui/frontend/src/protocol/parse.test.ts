import { describe, expect, it } from "vitest";

import { parseServerEvent } from "./parse";

const envelope = {
  session_id: "s1",
  generation: 1,
  sequence: 1,
};

const completeEvents = [
  {
    ...envelope,
    type: "session_snapshot",
    turn_id: null,
    messages: [{ role: "user", content: "hello" }],
    running: false,
    queued_message: null,
    safety: { mode: "default", sandbox: { backend: "none" } },
  },
  { ...envelope, type: "turn_started", turn_id: "t1", mode: "plan" },
  { ...envelope, type: "assistant_delta", turn_id: "t1", text: "hello" },
  { ...envelope, type: "stream_reset", turn_id: "t1" },
  {
    ...envelope,
    type: "activity_started",
    turn_id: "t1",
    activity_id: "a1",
    parent_activity_id: null,
    actor: "tool",
    name: "read_file",
    args: { path: "README.md" },
    started_at: "2026-08-08T00:00:00Z",
  },
  {
    ...envelope,
    type: "activity_completed",
    turn_id: "t1",
    activity_id: "a1",
    parent_activity_id: null,
    actor: "tool",
    name: "read_file",
    args: { path: "README.md" },
    result: { content: "ok" },
    is_error: false,
    started_at: "2026-08-08T00:00:00Z",
    duration_ms: 12,
  },
  {
    ...envelope,
    type: "permission_requested",
    turn_id: "t1",
    request_id: "p1",
    action: "write_file",
    scope: "README.md",
    reason: "Needs an edit",
  },
  {
    ...envelope,
    type: "permission_resolved",
    turn_id: "t1",
    request_id: "p1",
    answer: "yes",
  },
  {
    ...envelope,
    type: "plan_approval_requested",
    turn_id: "t1",
    request_id: "r1",
    plan: "1. Make the change",
  },
  {
    ...envelope,
    type: "plan_approval_resolved",
    turn_id: "t1",
    request_id: "r1",
    approved: true,
    feedback: "",
  },
  {
    ...envelope,
    type: "context_updated",
    turn_id: "t1",
    context: { mode: "compaction", used_tokens: 123 },
  },
  { ...envelope, type: "turn_stopping", turn_id: "t1" },
  {
    ...envelope,
    type: "turn_completed",
    turn_id: "t1",
    messages: [{ role: "assistant", content: "done" }],
    final_text: "done",
  },
  { ...envelope, type: "turn_cancelled", turn_id: "t1" },
  {
    ...envelope,
    type: "turn_failed",
    turn_id: "t1",
    error_category: "provider",
    message: "Connection failed",
  },
  {
    ...envelope,
    type: "safety_updated",
    turn_id: null,
    safety: { mode: "readOnly", sampling_ratio: 0.5, sandbox: { backend: "none" } },
  },
] as const;

describe("parseServerEvent", () => {
  it("accepts and preserves every server event discriminant and field", () => {
    for (const input of completeEvents) {
      expect(parseServerEvent(input)).toEqual({ ok: true, value: input });
    }
  });

  it.each([
    null,
    [],
    { ...envelope },
    { ...envelope, type: "future_event" },
    { ...envelope, type: "assistant_delta", turn_id: "t1" },
    { ...envelope, type: "assistant_delta", turn_id: "t1", text: "ok", extra: true },
  ])("rejects non-events, unknown types, missing fields, and extra fields", (input) => {
    expect(parseServerEvent(input).ok).toBe(false);
  });

  it.each([
    { ...completeEvents[2], generation: 0 },
    { ...completeEvents[2], generation: 1.5 },
    { ...completeEvents[2], generation: Number.MAX_SAFE_INTEGER + 1 },
    { ...completeEvents[2], sequence: true },
    { ...completeEvents[5], duration_ms: -1 },
    { ...completeEvents[5], duration_ms: Number.POSITIVE_INFINITY },
    { ...completeEvents[5], is_error: 0 },
  ])("rejects invalid bounded primitive values", (input) => {
    expect(parseServerEvent(input).ok).toBe(false);
  });

  it("rejects unsafe integer-valued numbers nested in authoritative JSON payloads", () => {
    const input = {
      ...completeEvents[15],
      safety: { limits: { tokens: Number.MAX_SAFE_INTEGER + 1 } },
    };

    expect(parseServerEvent(input).ok).toBe(false);
  });

  it("rejects sparse arrays in authoritative JSON payloads", () => {
    const sparse: unknown[] = [];
    sparse.length = 1;

    expect(
      parseServerEvent({ ...completeEvents[5], result: { nested: sparse } }).ok,
    ).toBe(false);
  });

  it("rejects arrays with non-index enumerable properties", () => {
    const withExtraProperty: unknown[] = ["value"];
    Object.defineProperty(withExtraProperty, "metadata", {
      value: "not an array index",
      enumerable: true,
    });

    expect(
      parseServerEvent({ ...completeEvents[5], result: { nested: withExtraProperty } }).ok,
    ).toBe(false);
  });

  it.each([
    { ...completeEvents[2], session_id: " " },
    { ...completeEvents[2], turn_id: "" },
    { ...completeEvents[4], activity_id: "\ud800" },
    { ...completeEvents[5], args: { nested: ["bad\udfffvalue"] } },
    { ...completeEvents[0], messages: [{ role: "assistant", content: "bad\ud800text" }] },
  ])("rejects blank identifiers and malformed Unicode recursively", (input) => {
    const result = parseServerEvent(input);
    expect(result.ok).toBe(false);
    if (!result.ok) {
      expect(result.error).not.toContain("bad");
    }
  });
});
