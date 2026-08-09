import { describe, expect, it } from "vitest";

import type { ActivityItem } from "../../protocol/types";
import { groupActivities } from "./groupActivities";

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

  it.each([
    ["user-facing narration", activity("narration", "complete", { actor: "assistant" })],
    ["permission", activity("permission_requested", "running", { actor: "permission" })],
    ["plan review", activity("plan_review", "running", { actor: "plan" })],
    ["subagent start or completion", activity("spawn_agent", "complete", { actor: "subagent" })],
    ["turn completion", activity("turn_completed", "complete", { actor: "system" })],
  ])("ends a routine group at a %s boundary", (_label, boundary) => {
    const grouped = groupActivities([
      activity("read_file"),
      boundary,
      activity("list_dir"),
    ]);

    expect(grouped).toHaveLength(3);
    expect(grouped[0].map((item) => item.name)).toEqual(["read_file"]);
    expect(grouped[1].map((item) => item.name)).toEqual([boundary.name]);
    expect(grouped[2].map((item) => item.name)).toEqual(["list_dir"]);
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
});
