import { describe, expect, it } from "vitest";

import type {
  ActivityItem,
  TranscriptState,
  TranscriptAssistantTimelineItem,
  TranscriptBoundaryTimelineItem,
} from "../../protocol/types";
import { event } from "../../protocol/fixtures";
import { emptyTranscript, transcriptReducer } from "../../protocol/reducer";
import { groupActivities } from "./groupActivities";
import type { TimelineMarker } from "./groupActivities";

function activity(
  name: string,
  status: ActivityItem["status"] = "complete",
  overrides: Partial<ActivityItem> = {},
): ActivityItem {
  return {
    activityId: `${name}-${String(overrides.actor ?? "tool")}`,
    turnId: "turn-1",
    parentActivityId: null,
    actor: "tool",
    name,
    args: {},
    startedAt: "2026-08-08T00:00:00Z",
    status,
    result: null,
    isError: status === "error",
    durationMs: 10,
    ...overrides,
  };
}

function groupTimeline(state: TranscriptState) {
  const resolved: Array<ActivityItem | TimelineMarker> = [];
  for (const item of state.timeline) {
    if (item.kind !== "activity") {
      resolved.push(item);
      continue;
    }
    const resolvedActivity = state.activities[item.activityId];
    if (resolvedActivity !== undefined) resolved.push(resolvedActivity);
  }
  return groupActivities(resolved);
}

describe("groupActivities", () => {
  it("groups adjacent routine work but gives an error its own terminating group", () => {
    const grouped = groupActivities([
      activity("read_file"),
      activity("list_dir"),
      activity("bash", "error"),
    ]);

    expect(grouped).toHaveLength(2);
    expect(grouped.map((group) => group.map((item) => item.name))).toEqual([
      ["read_file", "list_dir"],
      ["bash"],
    ]);
  });

  it("ends groups at real narration, permission, plan-review, and turn boundaries", () => {
    const narration: TranscriptAssistantTimelineItem = {
      kind: "assistant",
      text: "I checked this.",
      messageIndex: null,
    };
    const permission: TranscriptBoundaryTimelineItem = { kind: "boundary", boundary: "permission" };
    const planReview: TranscriptBoundaryTimelineItem = { kind: "boundary", boundary: "plan_review" };
    const turnCompletion: TranscriptBoundaryTimelineItem = {
      kind: "boundary",
      boundary: "turn_completion",
    };
    const grouped = groupActivities([
      activity("read_file"),
      narration,
      activity("list_dir"),
      permission,
      activity("read_file", "complete", { activityId: "read-after-permission" }),
      planReview,
      activity("list_dir", "complete", { activityId: "list-after-plan" }),
      turnCompletion,
    ]);

    expect(grouped).toEqual([
      [expect.objectContaining({ name: "read_file" })],
      narration,
      [expect.objectContaining({ name: "list_dir" })],
      permission,
      [expect.objectContaining({ name: "read_file" })],
      planReview,
      [expect.objectContaining({ name: "list_dir" })],
      turnCompletion,
    ]);
  });

  it("keeps real subagent and error activities as standalone boundaries", () => {
    const subagent = activity("spawn_agent", "complete", { actor: "subagent" });
    const failed = activity("bash", "error");

    expect(groupActivities([
      activity("read_file"),
      subagent,
      activity("list_dir"),
      failed,
      activity("read_file", "complete", { activityId: "read-after-error" }),
    ])).toEqual([
      [expect.objectContaining({ name: "read_file" })],
      [subagent],
      [expect.objectContaining({ name: "list_dir" })],
      [failed],
      [expect.objectContaining({ name: "read_file" })],
    ]);
  });

  it("does not group activities across turns or mutate the ordered input", () => {
    const first = activity("read_file");
    const second = activity("list_dir", "complete", { turnId: "turn-2" });
    const ordered = [first, second];

    const grouped = groupActivities(ordered);

    expect(grouped).toEqual([[first], [second]]);
    expect(ordered).toEqual([first, second]);
  });

  it("ends a subagent child group before routine root work resumes", () => {
    const subagent = activity("spawn_agent", "complete", {
      activityId: "subagent-1",
      actor: "subagent",
    });
    const childRead = activity("read_file", "complete", {
      activityId: "child-read",
      parentActivityId: "subagent-1",
    });
    const childList = activity("list_dir", "complete", {
      activityId: "child-list",
      parentActivityId: "subagent-1",
    });
    const rootRead = activity("read_file", "complete", {
      activityId: "root-read",
      parentActivityId: null,
    });

    expect(groupActivities([subagent, childRead, childList, rootRead])).toEqual([
      [subagent],
      [childRead, childList],
      [rootRead],
    ]);
  });

  it("does not group root work across a subagent completion event", () => {
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

    expect(groupTimeline(state)).toEqual([
      [expect.objectContaining({ activityId: "subagent" })],
      [expect.objectContaining({ activityId: "root-read" })],
      { kind: "boundary", boundary: "subagent_completion" },
      [expect.objectContaining({ activityId: "root-list" })],
    ]);
    expect(state.timeline.filter((item) => item.kind === "activity" && item.activityId === "subagent"))
      .toHaveLength(1);
  });

  it("does not group root work across an activity error completion event", () => {
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

    expect(groupTimeline(state)).toEqual([
      [expect.objectContaining({ activityId: "failed" })],
      [expect.objectContaining({ activityId: "root-read" })],
      { kind: "boundary", boundary: "activity_error" },
      [expect.objectContaining({ activityId: "root-list" })],
    ]);
    expect(state.timeline.filter((item) => item.kind === "activity" && item.activityId === "failed"))
      .toHaveLength(1);
  });

  it("ends routine activity grouping at an exact context-management marker", () => {
    let state = transcriptReducer(
      emptyTranscript(),
      event("activity_started", { sequence: 1, activity_id: "before-context", name: "read_file" }),
    );
    state = transcriptReducer(state, event("context_updated", {
      sequence: 2,
      context: { mode: "compaction", summarized_messages: 2 },
    }));
    state = transcriptReducer(
      state,
      event("activity_started", { sequence: 3, activity_id: "after-context", name: "list_dir" }),
    );

    expect(groupTimeline(state)).toEqual([
      [expect.objectContaining({ activityId: "before-context" })],
      expect.objectContaining({
        kind: "context",
        context: { mode: "compaction", summarized_messages: 2 },
      }),
      [expect.objectContaining({ activityId: "after-context" })],
    ]);
  });
});
