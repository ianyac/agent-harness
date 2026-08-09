import { describe, expect, it } from "vitest";

import { event } from "./fixtures";
import { emptyTranscript, transcriptReducer } from "./reducer";

function timelineOf(state: ReturnType<typeof emptyTranscript>): unknown[] | undefined {
  return (state as ReturnType<typeof emptyTranscript> & { timeline?: unknown[] }).timeline;
}

describe("transcriptReducer", () => {
  it("resets stale streamed text before a provider retry", () => {
    let state = transcriptReducer(emptyTranscript(), event("turn_started"));
    state = transcriptReducer(state, event("assistant_delta", { text: "stale" }));
    state = transcriptReducer(state, event("stream_reset"));
    state = transcriptReducer(state, event("assistant_delta", { text: "fresh" }));
    expect(state.streamingText).toBe("fresh");
  });

  it("replaces ephemera with authoritative completed messages", () => {
    let state = transcriptReducer(emptyTranscript(), event("turn_started", { sequence: 1 }));
    state = transcriptReducer(state, event("assistant_delta", { sequence: 2, text: "draft" }));
    state = transcriptReducer(state, event("permission_requested", { sequence: 3 }));
    state = transcriptReducer(
      state,
      event("turn_completed", {
        sequence: 4,
        messages: [{ role: "assistant", content: "authoritative" }],
      }),
    );
    expect(state.messages).toEqual([{ role: "assistant", content: "authoritative" }]);
    expect(state.streamingText).toBe("");
    expect(state.permission).toBeNull();
    expect(state.running).toBe(false);
  });

  it("retains real current-turn boundaries and reconciles narration in chronological order", () => {
    let state = transcriptReducer(emptyTranscript(), event("turn_started", { sequence: 1 }));
    state = transcriptReducer(
      state,
      event("activity_started", { sequence: 2, activity_id: "read-1", name: "read_file" }),
    );
    state = transcriptReducer(state, event("assistant_delta", { sequence: 3, text: "I found context." }));
    state = transcriptReducer(
      state,
      event("activity_started", { sequence: 4, activity_id: "read-2", name: "list_dir" }),
    );
    state = transcriptReducer(state, event("permission_requested", { sequence: 5 }));
    state = transcriptReducer(
      state,
      event("activity_started", { sequence: 6, activity_id: "write-1", name: "write_file" }),
    );
    state = transcriptReducer(state, event("plan_approval_requested", { sequence: 7 }));
    state = transcriptReducer(
      state,
      event("activity_started", {
        sequence: 8,
        activity_id: "agent-1",
        actor: "subagent",
        name: "spawn_agent",
      }),
    );
    state = transcriptReducer(
      state,
      event("activity_started", { sequence: 9, activity_id: "bash-1", name: "bash" }),
    );
    state = transcriptReducer(
      state,
      event("activity_completed", {
        sequence: 10,
        activity_id: "bash-1",
        name: "bash",
        is_error: true,
      }),
    );
    state = transcriptReducer(state, event("assistant_delta", { sequence: 11, text: "Draft result" }));
    state = transcriptReducer(
      state,
      event("turn_completed", {
        sequence: 12,
        final_text: "Authoritative result",
        messages: [
          { role: "assistant", content: "I found context.", tool_calls: [] },
          { role: "assistant", content: "Authoritative result" },
        ],
      }),
    );

    expect(timelineOf(state)).toEqual([
      { kind: "activity", activityId: "read-1" },
      { kind: "assistant", text: "I found context.", messageIndex: 0 },
      { kind: "activity", activityId: "read-2" },
      { kind: "boundary", boundary: "permission" },
      { kind: "activity", activityId: "write-1" },
      { kind: "boundary", boundary: "plan_review" },
      { kind: "activity", activityId: "agent-1" },
      { kind: "activity", activityId: "bash-1" },
      { kind: "boundary", boundary: "activity_error" },
      { kind: "assistant", text: "Authoritative result", messageIndex: 1 },
      { kind: "boundary", boundary: "turn_completion" },
    ]);
    expect(state.activities["bash-1"].status).toBe("error");
  });

  it("removes only the stale active narration on stream reset and resets timeline authority", () => {
    let state = transcriptReducer(emptyTranscript(), event("turn_started", { sequence: 1 }));
    state = transcriptReducer(state, event("assistant_delta", { sequence: 2, text: "stale" }));
    state = transcriptReducer(state, event("stream_reset", { sequence: 3 }));
    state = transcriptReducer(state, event("assistant_delta", { sequence: 4, text: "fresh" }));
    expect(timelineOf(state)).toEqual([
      { kind: "assistant", text: "fresh", messageIndex: null },
    ]);

    state = transcriptReducer(
      state,
      event("session_snapshot", {
        generation: 2,
        sequence: 1,
        messages: [{ role: "assistant", content: "snapshot" }],
      }),
    );
    expect(timelineOf(state)).toEqual([]);

    state = transcriptReducer(state, event("turn_started", { generation: 2, sequence: 2 }));
    expect(timelineOf(state)).toEqual([]);
  });

  it("reconciles repeated narration to the current turn rather than a historical match", () => {
    let state = transcriptReducer(
      emptyTranscript(),
      event("session_snapshot", {
        sequence: 1,
        messages: [
          { role: "user", content: "Earlier" },
          { role: "assistant", content: "Same narration" },
        ],
      }),
    );
    state = transcriptReducer(state, event("turn_started", { sequence: 2 }));
    state = transcriptReducer(state, event("assistant_delta", { sequence: 3, text: "Same narration" }));
    state = transcriptReducer(
      state,
      event("activity_started", { sequence: 4, activity_id: "read-current" }),
    );
    state = transcriptReducer(state, event("assistant_delta", { sequence: 5, text: "Draft final" }));
    state = transcriptReducer(
      state,
      event("turn_completed", {
        sequence: 6,
        final_text: "Final",
        messages: [
          { role: "user", content: "Earlier" },
          { role: "assistant", content: "Same narration" },
          { role: "user", content: "Current" },
          { role: "assistant", content: "Same narration", tool_calls: [] },
          { role: "assistant", content: "Final" },
        ],
      }),
    );

    expect(timelineOf(state)).toEqual([
      { kind: "assistant", text: "Same narration", messageIndex: 3 },
      { kind: "activity", activityId: "read-current" },
      { kind: "assistant", text: "Final", messageIndex: 4 },
      { kind: "boundary", boundary: "turn_completion" },
    ]);
  });

  it("replaces every changed current-turn narration by authoritative position", () => {
    let state = transcriptReducer(
      emptyTranscript(),
      event("session_snapshot", {
        sequence: 1,
        messages: [
          { role: "user", content: "Earlier" },
          { role: "assistant", content: "Draft before" },
          { role: "assistant", content: "Historical different" },
          { role: "user", content: "Current" },
        ],
      }),
    );
    state = transcriptReducer(state, event("turn_started", { sequence: 2 }));
    state = transcriptReducer(state, event("assistant_delta", { sequence: 3, text: "Draft before" }));
    state = transcriptReducer(
      state,
      event("activity_started", { sequence: 4, activity_id: "read-current" }),
    );
    state = transcriptReducer(state, event("assistant_delta", { sequence: 5, text: "Draft after" }));
    state = transcriptReducer(
      state,
      event("turn_completed", {
        sequence: 6,
        final_text: "Authoritative after",
        messages: [
          { role: "user", content: "Earlier" },
          { role: "assistant", content: "Draft before" },
          { role: "assistant", content: "Historical different" },
          { role: "user", content: "Current" },
          { role: "assistant", content: "Authoritative before" },
          { role: "assistant", content: "Authoritative after" },
        ],
      }),
    );

    expect(timelineOf(state)).toEqual([
      { kind: "assistant", text: "Authoritative before", messageIndex: 4 },
      { kind: "activity", activityId: "read-current" },
      { kind: "assistant", text: "Authoritative after", messageIndex: 5 },
      { kind: "boundary", boundary: "turn_completion" },
    ]);
    expect(state.messages).toEqual([
      { role: "user", content: "Earlier" },
      { role: "assistant", content: "Draft before" },
      { role: "assistant", content: "Historical different" },
      { role: "user", content: "Current" },
      { role: "assistant", content: "Authoritative before" },
      { role: "assistant", content: "Authoritative after" },
    ]);
  });

  it("discards surplus draft segments when completion has fewer authoritative messages", () => {
    let state = transcriptReducer(emptyTranscript(), event("turn_started", { sequence: 1 }));
    state = transcriptReducer(state, event("assistant_delta", { sequence: 2, text: "Draft one" }));
    state = transcriptReducer(
      state,
      event("activity_started", { sequence: 3, activity_id: "read-one" }),
    );
    state = transcriptReducer(state, event("assistant_delta", { sequence: 4, text: "Draft two" }));
    state = transcriptReducer(
      state,
      event("activity_started", { sequence: 5, activity_id: "read-two" }),
    );
    state = transcriptReducer(state, event("assistant_delta", { sequence: 6, text: "Draft surplus" }));
    state = transcriptReducer(
      state,
      event("turn_completed", {
        sequence: 7,
        final_text: "Authoritative two",
        messages: [
          { role: "user", content: "Current" },
          { role: "assistant", content: "Authoritative one" },
          { role: "assistant", content: "Authoritative two" },
        ],
      }),
    );

    expect(timelineOf(state)).toEqual([
      { kind: "assistant", text: "Authoritative one", messageIndex: 1 },
      { kind: "activity", activityId: "read-one" },
      { kind: "assistant", text: "Authoritative two", messageIndex: 2 },
      { kind: "activity", activityId: "read-two" },
      { kind: "boundary", boundary: "turn_completion" },
    ]);
  });

  it("appends surplus authoritative messages after the known current-turn timeline", () => {
    let state = transcriptReducer(emptyTranscript(), event("turn_started", { sequence: 1 }));
    state = transcriptReducer(state, event("assistant_delta", { sequence: 2, text: "Only draft" }));
    state = transcriptReducer(
      state,
      event("activity_started", { sequence: 3, activity_id: "read-current" }),
    );
    state = transcriptReducer(
      state,
      event("turn_completed", {
        sequence: 4,
        final_text: "Authoritative two",
        messages: [
          { role: "user", content: "Current" },
          { role: "assistant", content: "Authoritative one" },
          { role: "assistant", content: "Authoritative two" },
        ],
      }),
    );

    expect(timelineOf(state)).toEqual([
      { kind: "assistant", text: "Authoritative one", messageIndex: 1 },
      { kind: "activity", activityId: "read-current" },
      { kind: "assistant", text: "Authoritative two", messageIndex: 2 },
      { kind: "boundary", boundary: "turn_completion" },
    ]);
  });

  it("does not let an empty draft segment consume an authoritative position", () => {
    let state = transcriptReducer(emptyTranscript(), event("turn_started", { sequence: 1 }));
    state = transcriptReducer(state, event("assistant_delta", { sequence: 2, text: "" }));
    state = transcriptReducer(
      state,
      event("activity_started", { sequence: 3, activity_id: "read-current" }),
    );
    state = transcriptReducer(state, event("assistant_delta", { sequence: 4, text: "Real draft" }));
    state = transcriptReducer(
      state,
      event("turn_completed", {
        sequence: 5,
        final_text: "Authoritative narration",
        messages: [
          { role: "user", content: "Current" },
          { role: "assistant", content: "Authoritative narration" },
        ],
      }),
    );

    expect(timelineOf(state)).toEqual([
      { kind: "activity", activityId: "read-current" },
      { kind: "assistant", text: "Authoritative narration", messageIndex: 1 },
      { kind: "boundary", boundary: "turn_completion" },
    ]);
  });

  it("skips visually empty structured messages before positional reconciliation", () => {
    let state = transcriptReducer(emptyTranscript(), event("turn_started", { sequence: 1 }));
    state = transcriptReducer(state, event("assistant_delta", { sequence: 2, text: "Draft before" }));
    state = transcriptReducer(
      state,
      event("activity_started", { sequence: 3, activity_id: "read-current" }),
    );
    state = transcriptReducer(state, event("assistant_delta", { sequence: 4, text: "Draft after" }));
    state = transcriptReducer(
      state,
      event("turn_completed", {
        sequence: 5,
        final_text: "Authoritative after",
        messages: [
          { role: "user", content: "Current" },
          { role: "assistant", content: [{ type: "text", text: "" }] },
          { role: "assistant", content: [{ type: "text", text: "Authoritative before" }] },
          {
            role: "assistant",
            content: [{ type: "text", text: "" }, { type: "text", text: "" }],
          },
          { role: "assistant", content: [{ type: "text", text: "Authoritative after" }] },
        ],
      }),
    );

    expect(timelineOf(state)).toEqual([
      { kind: "assistant", text: "Authoritative before", messageIndex: 2 },
      { kind: "activity", activityId: "read-current" },
      { kind: "assistant", text: "Authoritative after", messageIndex: 4 },
      { kind: "boundary", boundary: "turn_completion" },
    ]);
  });

  it("rejects stale generations, duplicates, and out-of-order sequences", () => {
    let state = transcriptReducer(
      emptyTranscript(),
      event("session_snapshot", { generation: 3, sequence: 1 }),
    );
    state = transcriptReducer(state, event("assistant_delta", { generation: 3, sequence: 2, text: "kept" }));
    const accepted = state;

    expect(transcriptReducer(state, event("stream_reset", { generation: 2, sequence: 99 }))).toBe(accepted);
    expect(transcriptReducer(state, event("stream_reset", { generation: 3, sequence: 2 }))).toBe(accepted);
    expect(transcriptReducer(state, event("stream_reset", { generation: 3, sequence: 1 }))).toBe(accepted);
    expect(transcriptReducer(state, event("stream_reset", { generation: 4, sequence: 1 }))).toBe(accepted);
  });

  it("preserves activity parentage and completion details in first-seen order", () => {
    let state = transcriptReducer(
      emptyTranscript(),
      event("activity_started", { sequence: 1, activity_id: "parent", name: "agent" }),
    );
    state = transcriptReducer(
      state,
      event("activity_started", {
        sequence: 2,
        activity_id: "child",
        parent_activity_id: "parent",
        name: "read_file",
      }),
    );
    state = transcriptReducer(
      state,
      event("activity_completed", {
        sequence: 3,
        activity_id: "child",
        parent_activity_id: "parent",
        name: "read_file",
        result: { content: "ok" },
        duration_ms: 12,
      }),
    );

    expect(state.activityOrder).toEqual(["parent", "child"]);
    expect(state.activities.child).toMatchObject({
      parentActivityId: "parent",
      status: "complete",
      result: { content: "ok" },
      durationMs: 12,
    });
  });

  it("records subagent completion at its later event position", () => {
    let state = transcriptReducer(
      emptyTranscript(),
      event("activity_started", {
        sequence: 1,
        activity_id: "subagent",
        actor: "subagent",
        name: "spawn_agent",
      }),
    );
    state = transcriptReducer(
      state,
      event("activity_started", { sequence: 2, activity_id: "root-read", name: "read_file" }),
    );
    state = transcriptReducer(
      state,
      event("activity_completed", {
        sequence: 3,
        activity_id: "subagent",
        actor: "subagent",
        name: "spawn_agent",
      }),
    );
    state = transcriptReducer(
      state,
      event("activity_started", { sequence: 4, activity_id: "root-list", name: "list_dir" }),
    );

    expect(state.activityOrder).toEqual(["subagent", "root-read", "root-list"]);
    expect(timelineOf(state)).toEqual([
      { kind: "activity", activityId: "subagent" },
      { kind: "activity", activityId: "root-read" },
      { kind: "boundary", boundary: "subagent_completion" },
      { kind: "activity", activityId: "root-list" },
    ]);
  });

  it("records activity error completion at its later event position", () => {
    let state = transcriptReducer(
      emptyTranscript(),
      event("activity_started", { sequence: 1, activity_id: "failed", name: "bash" }),
    );
    state = transcriptReducer(
      state,
      event("activity_started", { sequence: 2, activity_id: "root-read", name: "read_file" }),
    );
    state = transcriptReducer(
      state,
      event("activity_completed", {
        sequence: 3,
        activity_id: "failed",
        name: "bash",
        is_error: true,
      }),
    );
    state = transcriptReducer(
      state,
      event("activity_started", { sequence: 4, activity_id: "root-list", name: "list_dir" }),
    );

    expect(state.activityOrder).toEqual(["failed", "root-read", "root-list"]);
    expect(timelineOf(state)).toEqual([
      { kind: "activity", activityId: "failed" },
      { kind: "activity", activityId: "root-read" },
      { kind: "boundary", boundary: "activity_error" },
      { kind: "activity", activityId: "root-list" },
    ]);
  });

  it("keeps only the current permission and plan-review requests authoritative", () => {
    let state = transcriptReducer(emptyTranscript(), event("permission_requested", { sequence: 1, request_id: "p1" }));
    state = transcriptReducer(state, event("permission_requested", { sequence: 2, request_id: "p2" }));
    state = transcriptReducer(state, event("permission_resolved", { sequence: 3, request_id: "p1" }));
    expect(state.permission?.requestId).toBe("p2");
    state = transcriptReducer(state, event("permission_resolved", { sequence: 4, request_id: "p2" }));
    expect(state.permission).toBeNull();

    state = transcriptReducer(state, event("plan_approval_requested", { sequence: 5, request_id: "r1" }));
    state = transcriptReducer(state, event("plan_approval_requested", { sequence: 6, request_id: "r2" }));
    state = transcriptReducer(state, event("plan_approval_resolved", { sequence: 7, request_id: "r1" }));
    expect(state.planReview?.requestId).toBe("r2");
    state = transcriptReducer(state, event("plan_approval_resolved", { sequence: 8, request_id: "r2" }));
    expect(state.planReview).toBeNull();
  });

  it("restores queue state from snapshots and consumes it when the next turn starts", () => {
    let state = transcriptReducer(
      emptyTranscript(),
      event("session_snapshot", {
        sequence: 1,
        running: true,
        queued_message: { type: "queue_message", text: "next", mode: "plan" },
      }),
    );
    expect(state.queued).toEqual({ type: "queue_message", text: "next", mode: "plan" });
    state = transcriptReducer(state, event("turn_started", { sequence: 2, mode: "plan" }));
    expect(state.queued).toBeNull();
  });

  it("clears transient state when a turn is cancelled", () => {
    let state = transcriptReducer(emptyTranscript(), event("turn_started", { sequence: 1 }));
    state = transcriptReducer(state, event("assistant_delta", { sequence: 2, text: "partial" }));
    state = transcriptReducer(state, event("turn_stopping", { sequence: 3 }));
    expect(state.stopping).toBe(true);
    state = transcriptReducer(state, event("turn_cancelled", { sequence: 4 }));
    expect(state).toMatchObject({ running: false, stopping: false, streamingText: "", error: null });
  });

  it("surfaces a recoverable failure and clears it at the next turn", () => {
    let state = transcriptReducer(
      emptyTranscript(),
      event("turn_failed", { sequence: 1, error_category: "provider", message: "offline" }),
    );
    expect(state.error).toEqual({ category: "provider", message: "offline" });
    expect(state.running).toBe(false);
    state = transcriptReducer(state, event("turn_started", { sequence: 2 }));
    expect(state.error).toBeNull();
  });

  it("replaces safety state without discarding transcript state", () => {
    let state = transcriptReducer(emptyTranscript(), event("assistant_delta", { sequence: 1, text: "keep" }));
    state = transcriptReducer(
      state,
      event("safety_updated", { sequence: 2, safety: { mode: "readOnly", nested: { enabled: true } } }),
    );
    expect(state.safety).toEqual({ mode: "readOnly", nested: { enabled: true } });
    expect(state.streamingText).toBe("keep");
  });

  it("replaces all prior state from a newer authoritative snapshot", () => {
    let state = transcriptReducer(emptyTranscript(), event("turn_started", { sequence: 1 }));
    state = transcriptReducer(state, event("assistant_delta", { sequence: 2, text: "stale" }));
    state = transcriptReducer(state, event("activity_started", { sequence: 3 }));
    state = transcriptReducer(
      state,
      event("session_snapshot", {
        generation: 2,
        sequence: 1,
        messages: [{ role: "assistant", content: "snapshot" }],
        running: false,
        queued_message: null,
        safety: { mode: "default" },
      }),
    );

    expect(state).toEqual({
      generation: 2,
      lastSequence: 1,
      messages: [{ role: "assistant", content: "snapshot" }],
      streamingText: "",
      activities: {},
      activityOrder: [],
      timeline: [],
      permission: null,
      planReview: null,
      running: false,
      stopping: false,
      queued: null,
      safety: { mode: "default" },
      error: null,
    });
  });
});
