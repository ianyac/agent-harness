import { describe, expect, it } from "vitest";

import { messageHistory } from "./history";
import type { HarnessMessage } from "./types";

function toolCallMessage(
  calls: ReadonlyArray<{ id: string; name: string; args?: string }>,
  content: string | null = null,
): HarnessMessage {
  return {
    role: "assistant",
    content,
    tool_calls: calls.map((call) => ({
      id: call.id,
      type: "function",
      function: { name: call.name, arguments: call.args ?? "{}" },
    })),
  };
}

describe("messageHistory", () => {
  it("derives grouped activities with results from authoritative messages", () => {
    const history = messageHistory([
      { role: "user", content: "read" },
      toolCallMessage([{ id: "call_1", name: "read_file", args: "{\"path\": \"notes.txt\"}" }]),
      { role: "tool", tool_call_id: "call_1", content: "Meeting notes" },
      { role: "assistant", content: "Done." },
    ]);

    expect(history.groups).toHaveLength(1);
    expect(history.groups[0].startIndex).toBe(1);
    expect(history.groups[0].activities).toEqual([
      expect.objectContaining({
        activityId: "history:1:call_1",
        name: "read_file",
        actor: "tool",
        args: { path: "notes.txt" },
        status: "complete",
        result: "Meeting notes",
      }),
    ]);
    expect(history.byId.get("history:1:call_1")?.result).toBe("Meeting notes");
    expect(history.groupsByStartIndex.get(1)).toBe(history.groups[0]);
  });

  it("merges consecutive tool steps into one group and breaks at prose", () => {
    const history = messageHistory([
      { role: "user", content: "many" },
      toolCallMessage([
        { id: "call_1", name: "read_file" },
        { id: "call_2", name: "read_file" },
      ]),
      { role: "tool", tool_call_id: "call_1", content: "one" },
      { role: "tool", tool_call_id: "call_2", content: "two" },
      toolCallMessage([{ id: "call_3", name: "list_dir" }]),
      { role: "tool", tool_call_id: "call_3", content: "three" },
      { role: "assistant", content: "Summary." },
      toolCallMessage([{ id: "call_4", name: "bash" }]),
      { role: "tool", tool_call_id: "call_4", content: "four" },
    ]);

    expect(history.groups.map((group) => group.activities.map((item) => item.result))).toEqual([
      ["one", "two", "three"],
      ["four"],
    ]);
    expect(history.groups.map((group) => group.startIndex)).toEqual([1, 7]);
  });

  it("keeps recurring tool_call ids scoped to their own group", () => {
    const history = messageHistory([
      { role: "user", content: "first" },
      toolCallMessage([{ id: "call_1", name: "read_file" }]),
      { role: "tool", tool_call_id: "call_1", content: "first result" },
      { role: "assistant", content: "Done." },
      { role: "user", content: "second" },
      toolCallMessage([{ id: "call_1", name: "bash" }]),
      { role: "tool", tool_call_id: "call_1", content: "second result" },
      { role: "assistant", content: "Done again." },
    ]);

    expect(history.groups).toHaveLength(2);
    expect(history.groups[0].activities[0].result).toBe("first result");
    expect(history.groups[1].activities[0].result).toBe("second result");
    expect(history.groups[1].activities[0].activityId).toBe("history:5:call_1");
  });

  it("anchors a prose-plus-tool-calls message at its own index", () => {
    const history = messageHistory([
      { role: "user", content: "go" },
      toolCallMessage([{ id: "call_1", name: "read_file" }], "Let me check."),
      { role: "tool", tool_call_id: "call_1", content: "result" },
    ]);

    expect(history.groups).toHaveLength(1);
    expect(history.groups[0].startIndex).toBe(1);
  });

  it("labels agent calls as subagent work", () => {
    const history = messageHistory([
      toolCallMessage([{ id: "call_1", name: "agent" }]),
      { role: "tool", tool_call_id: "call_1", content: "done" },
    ]);
    expect(history.groups[0].activities[0].actor).toBe("subagent");
  });

  it("tolerates malformed arguments, missing results, and unknown shapes", () => {
    const history = messageHistory([
      toolCallMessage([{ id: "call_1", name: "bash", args: "not json" }]),
      { role: "assistant", content: null, tool_calls: "bogus" },
      { role: "tool", tool_call_id: 7, content: "orphan" },
      { role: "tool", content: "no id" },
    ]);

    expect(history.groups).toHaveLength(1);
    expect(history.groups[0].activities[0]).toMatchObject({
      args: {},
      status: "complete",
    });
    expect(history.groups[0].activities[0].result).toBeUndefined();
  });

  it("returns nothing for prose-only conversations", () => {
    const history = messageHistory([
      { role: "user", content: "hello" },
      { role: "assistant", content: "hi" },
    ]);
    expect(history.groups).toHaveLength(0);
    expect(history.byId.size).toBe(0);
  });
});
