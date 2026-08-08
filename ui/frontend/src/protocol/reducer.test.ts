import { describe, expect, it } from "vitest";

import { event } from "./fixtures";
import { emptyTranscript, transcriptReducer } from "./reducer";

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
