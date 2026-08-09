import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { ActivityItem, HarnessMessage, TranscriptState } from "../../protocol/types";
import { event } from "../../protocol/fixtures";
import { emptyTranscript, transcriptReducer } from "../../protocol/reducer";
import { Conversation } from "./Conversation";

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

function activity(overrides: Partial<ActivityItem> = {}): ActivityItem {
  return {
    activityId: "activity-1",
    turnId: "turn-1",
    parentActivityId: null,
    actor: "tool",
    name: "bash",
    args: { command: "npm test" },
    startedAt: "2026-08-08T00:00:00Z",
    status: "complete",
    result: "18 tests passed\nComplete output remains available.",
    isError: false,
    durationMs: 34_000,
    ...overrides,
  };
}

function transcript(overrides: Partial<TranscriptState> = {}): TranscriptState {
  return {
    generation: 1,
    lastSequence: 1,
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
    error: null,
    ...overrides,
    latestContext: overrides.latestContext ?? null,
    terminal: overrides.terminal ?? null,
    activeTurn: overrides.activeTurn ?? null,
  };
}

function messages(...items: HarnessMessage[]): HarnessMessage[] {
  return items;
}

describe("Conversation", () => {
  it("renders user and assistant messages with distinct, labelled alignment", () => {
    render(
      <Conversation
        state={transcript({
          messages: messages(
            { role: "user", content: "Please inspect this." },
            { role: "assistant", content: "I found the cause." },
          ),
        })}
        openInspector={() => {}}
      />,
    );

    const userMessage = screen.getByRole("article", { name: "User message" });
    const assistantMessage = screen.getByRole("article", { name: "Assistant message" });
    expect(userMessage).toHaveAttribute("data-message-role", "user");
    expect(assistantMessage).toHaveAttribute("data-message-role", "assistant");
    expect(userMessage).toHaveTextContent("Please inspect this.");
    expect(assistantMessage).toHaveTextContent("I found the cause.");
  });

  it("uses context markers as grouping boundaries without rendering them as assistant prose", () => {
    let state = transcriptReducer(emptyTranscript(), event("activity_started", {
      sequence: 1,
      activity_id: "before-context",
      name: "read_file",
    }));
    state = transcriptReducer(state, event("context_updated", {
      sequence: 2,
      context: { mode: "compaction", summarized_messages: 3 },
    }));
    state = transcriptReducer(state, event("activity_started", {
      sequence: 3,
      activity_id: "after-context",
      name: "list_dir",
    }));

    render(<Conversation state={state} openInspector={() => {}} />);

    expect(screen.getAllByRole("button", { name: /Open activity:/ })).toHaveLength(2);
    expect(screen.queryByText(/summarized_messages/)).not.toBeInTheDocument();
    expect(screen.queryByRole("article", { name: "Assistant message" })).not.toBeInTheDocument();
  });

  it("renders safe Markdown links without executing raw HTML", () => {
    const rawMarkup = '<script>window.__task5Executed = true</script><b id="unsafe-bold">unsafe</b>';
    render(
      <Conversation
        state={transcript({
          messages: messages({
            role: "assistant",
            content: `See [the docs](https://example.com/docs).\n\n${rawMarkup}`,
          }),
        })}
        openInspector={() => {}}
      />,
    );

    expect(screen.getByRole("link", { name: "the docs" })).toHaveAttribute("target", "_blank");
    expect(screen.getByRole("link", { name: "the docs" })).toHaveAttribute("rel", "noreferrer");
    expect(document.querySelector("script")).toBeNull();
    expect(document.querySelector("#unsafe-bold")).toBeNull();
    expect(screen.getByText(/<script>window\.__task5Executed/)).toBeVisible();
  });

  it("keeps local Markdown paths selectable and copyable without a Reveal action", async () => {
    const user = userEvent.setup();
    const copyText = vi.fn().mockResolvedValue(undefined);
    render(
      <Conversation
        state={transcript({
          messages: messages({
            role: "assistant",
            content: "Open [the reducer](/Users/yc/work/ui/src/reducer.ts).",
          }),
        })}
        openInspector={() => {}}
        copyText={copyText}
      />,
    );

    expect(screen.queryByRole("link", { name: "the reducer" })).not.toBeInTheDocument();
    expect(screen.getByText("/Users/yc/work/ui/src/reducer.ts")).toBeVisible();
    expect(screen.queryByRole("button", { name: /reveal/i })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Copy path" }));
    expect(copyText).toHaveBeenCalledWith("/Users/yc/work/ui/src/reducer.ts");
    expect(screen.getByRole("button", { name: "Path copied" })).toBeVisible();
  });

  it("distinguishes safe local path forms, protocol-relative links, and unsafe schemes", async () => {
    const user = userEvent.setup();
    const copied: string[] = [];
    render(
      <Conversation
        state={transcript({
          messages: messages({
            role: "assistant",
            content: [
              "[file URL](<file:///tmp/a.ts>)",
              "[network path](//example.com/docs)",
              "[dot path](./src/a.ts)",
              "[parent path](../README.md)",
              String.raw`[Windows path](<C:\work\app.ts>)`,
              "[unsafe script](javascript:alert(1))",
              "[unsafe data](data:text/html,boom)",
            ].join("\n\n"),
          }),
        })}
        openInspector={() => {}}
        copyText={(text) => {
          copied.push(text);
        }}
      />,
    );

    const external = screen.getByRole("link", { name: "network path" });
    expect(external).toHaveAttribute("href", "//example.com/docs");
    expect(external).toHaveAttribute("target", "_blank");
    expect(external).toHaveAttribute("rel", "noreferrer");
    expect(screen.queryByRole("link", { name: "file URL" })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "dot path" })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "parent path" })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Windows path" })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "unsafe script" })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "unsafe data" })).not.toBeInTheDocument();
    expect(screen.getByText("file:///tmp/a.ts")).toBeVisible();
    expect(screen.getByText("./src/a.ts")).toBeVisible();
    expect(screen.getByText("../README.md")).toBeVisible();
    expect(screen.getByText(String.raw`C:\work\app.ts`)).toBeVisible();

    const copyButtons = screen.getAllByRole("button", { name: "Copy path" });
    expect(copyButtons).toHaveLength(4);
    for (const button of copyButtons) await user.click(button);
    expect(copied).toEqual([
      "file:///tmp/a.ts",
      "./src/a.ts",
      "../README.md",
      String.raw`C:\work\app.ts`,
    ]);
  });

  it("copies fenced code and gives diff additions, removals, and hunks semantic line classes", async () => {
    const user = userEvent.setup();
    const copyText = vi.fn().mockResolvedValue(undefined);
    render(
      <Conversation
        state={transcript({
          messages: messages({
            role: "assistant",
            content: [
              "```ts",
              "const answer = 42;",
              "```",
              "",
              "```diff",
              "--- a/value.ts",
              "+++ b/value.ts",
              "@@ -1 +1 @@",
              "-const answer = 41;",
              "+const answer = 42;",
              "```",
            ].join("\n"),
          }),
        })}
        openInspector={() => {}}
        copyText={copyText}
      />,
    );

    const codeRegion = screen.getByRole("region", { name: "TypeScript code" });
    await user.click(within(codeRegion).getByRole("button", { name: "Copy code" }));
    expect(copyText).toHaveBeenCalledWith("const answer = 42;");

    const diff = screen.getByRole("region", { name: "Diff" });
    expect(within(diff).getByText("@@ -1 +1 @@")).toHaveAttribute("data-diff-line", "hunk");
    expect(within(diff).getByText("-const answer = 41;")).toHaveAttribute(
      "data-diff-line",
      "removal",
    );
    expect(within(diff).getByText("+const answer = 42;")).toHaveAttribute(
      "data-diff-line",
      "addition",
    );
    expect(within(diff).getByText("--- a/value.ts")).toHaveAttribute("data-diff-line", "meta");
    expect(within(diff).getByText("+++ b/value.ts")).toHaveAttribute("data-diff-line", "meta");
  });

  it("announces a rejected code copy and allows a successful retry", async () => {
    const user = userEvent.setup();
    let attempts = 0;
    const copyText = async () => {
      attempts += 1;
      if (attempts === 1) throw new Error("clipboard denied");
    };
    render(
      <Conversation
        state={transcript({
          messages: messages({ role: "assistant", content: "```ts\nconst retry = true;\n```" }),
        })}
        openInspector={() => {}}
        copyText={copyText}
      />,
    );

    await user.click(screen.getByRole("button", { name: "Copy code" }));
    const codeStatus = screen.getByRole("status", { name: "Code copy status" });
    expect(codeStatus).toHaveTextContent("Copy failed. Try again.");
    expect(codeStatus).toHaveAttribute("aria-live", "polite");
    await user.click(screen.getByRole("button", { name: "Retry copy code" }));
    expect(screen.getByRole("button", { name: "Code copied" })).toBeVisible();
    expect(screen.getByRole("status", { name: "Code copy status" })).toHaveTextContent("Copied");
  });

  it("announces a rejected path copy and allows a successful retry", async () => {
    const user = userEvent.setup();
    let attempts = 0;
    const copyText = async () => {
      attempts += 1;
      if (attempts === 1) throw new Error("clipboard denied");
    };
    render(
      <Conversation
        state={transcript({
          messages: messages({ role: "assistant", content: "[file](./src/retry.ts)" }),
        })}
        openInspector={() => {}}
        copyText={copyText}
      />,
    );

    await user.click(screen.getByRole("button", { name: "Copy path" }));
    const pathStatus = screen.getByRole("status", { name: "Path copy status" });
    expect(pathStatus).toHaveTextContent("Copy failed. Try again.");
    expect(pathStatus).toHaveAttribute("aria-live", "polite");
    await user.click(screen.getByRole("button", { name: "Retry copy path" }));
    expect(screen.getByRole("button", { name: "Path copied" })).toBeVisible();
    expect(screen.getByRole("status", { name: "Path copy status" })).toHaveTextContent("Copied");
  });

  it("appends streaming text and then replaces it with the authoritative completed message", () => {
    const { rerender } = render(
      <Conversation
        state={transcript({
          messages: messages({ role: "user", content: "Question" }),
          streamingText: "Draft answer",
          running: true,
        })}
        openInspector={() => {}}
      />,
    );

    expect(screen.getByText("Draft answer").closest("article")).toHaveAttribute("data-streaming", "true");
    expect(screen.getByRole("log", { name: "Conversation transcript" })).toHaveAttribute("aria-live", "off");
    rerender(
      <Conversation
        state={transcript({
          lastSequence: 2,
          messages: messages(
            { role: "user", content: "Question" },
            { role: "assistant", content: "Authoritative answer" },
          ),
          streamingText: "",
          terminal: {
            kind: "completed",
            generation: 1,
            sequence: 2,
            turnId: "turn-complete",
            submissionId: null,
          },
        })}
        openInspector={() => {}}
      />,
    );

    expect(screen.queryByText("Draft answer")).not.toBeInTheDocument();
    expect(screen.getByText("Authoritative answer")).toBeVisible();
    expect(screen.getByRole("status", { name: "Conversation update" })).toHaveTextContent("Response complete");
  });

  it("keeps an authoritative replacement after the activity where its stream appeared", () => {
    const work = activity({ activityId: "activity-order", result: "read complete" });
    const running = {
      ...transcript({
        messages: messages({ role: "user", content: "Question" }),
        activities: { [work.activityId]: work },
        activityOrder: [work.activityId],
        streamingText: "Draft answer",
        running: true,
      }),
      timeline: [
        { kind: "activity", activityId: work.activityId },
        { kind: "assistant", text: "Draft answer", messageIndex: null },
      ],
    } as TranscriptState;
    const { rerender } = render(<Conversation state={running} openInspector={() => {}} />);

    const runningCard = screen.getByRole("button", { name: /Open activity/ });
    const draft = screen.getByText("Draft answer").closest("article");
    expect(runningCard.compareDocumentPosition(draft as Node) & Node.DOCUMENT_POSITION_FOLLOWING).not.toBe(0);

    const completed = {
      ...transcript({
        lastSequence: 2,
        messages: messages(
          { role: "user", content: "Question" },
          { role: "assistant", content: "Authoritative answer" },
        ),
        activities: { [work.activityId]: work },
        activityOrder: [work.activityId],
      }),
      timeline: [
        { kind: "activity", activityId: work.activityId },
        { kind: "assistant", text: "Authoritative answer", messageIndex: 1 },
        { kind: "boundary", boundary: "turn_completion" },
      ],
    } as TranscriptState;
    rerender(<Conversation state={completed} openInspector={() => {}} />);

    const completedCard = screen.getByRole("button", { name: /Open activity/ });
    const answer = screen.getByText("Authoritative answer").closest("article");
    expect(completedCard.compareDocumentPosition(answer as Node) & Node.DOCUMENT_POSITION_FOLLOWING).not.toBe(0);
    expect(screen.queryByText("Draft answer")).not.toBeInTheDocument();
  });

  it("renders every changed authoritative narration once at its streamed position", () => {
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
      event("activity_started", { sequence: 4, activity_id: "read-current", name: "read_file" }),
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
    render(<Conversation state={state} openInspector={() => {}} />);

    const before = screen.getByText("Authoritative before").closest("article");
    const work = screen.getByRole("button", { name: /Open activity/ });
    const after = screen.getByText("Authoritative after").closest("article");
    expect((before?.compareDocumentPosition(work) ?? 0) & Node.DOCUMENT_POSITION_FOLLOWING).not.toBe(0);
    expect(work.compareDocumentPosition(after as Node) & Node.DOCUMENT_POSITION_FOLLOWING).not.toBe(0);
    expect(screen.getAllByText("Authoritative before")).toHaveLength(1);
    expect(screen.getAllByText("Authoritative after")).toHaveLength(1);
    expect(screen.getAllByText("Draft before")).toHaveLength(1);
    expect(screen.queryByText("Draft after")).not.toBeInTheDocument();
  });

  it("renders persistent decisions at their chronological transcript anchors", () => {
    let state = transcriptReducer(emptyTranscript(), event("turn_started", { sequence: 1 }));
    state = transcriptReducer(state, event("assistant_delta", { sequence: 2, text: "Before permission" }));
    state = transcriptReducer(state, event("permission_requested", {
      sequence: 3,
      request_id: "anchored-permission",
      action: "write_file",
      scope: '{"path":"README.md"}',
      reason: "Update the project readme",
    }));
    state = transcriptReducer(state, event("permission_resolved", {
      sequence: 4,
      request_id: "anchored-permission",
      answer: "yes",
    }));
    state = transcriptReducer(state, event("activity_started", {
      sequence: 5,
      activity_id: "work-between-decisions",
      name: "write_file",
    }));
    state = transcriptReducer(state, event("plan_approval_requested", {
      sequence: 6,
      request_id: "anchored-plan",
      plan: "## Next steps\n\n1. Verify the change",
    }));
    render(<Conversation state={state} openInspector={() => {}} onSessionEvent={() => {}} />);

    const narration = screen.getByText("Before permission").closest("article");
    const permission = screen.getByRole("group", { name: "Permission review" });
    const work = screen.getByRole("button", { name: /Open activity/ });
    const plan = screen.getByRole("group", { name: "Plan review" });
    expect((narration?.compareDocumentPosition(permission) ?? 0) & Node.DOCUMENT_POSITION_FOLLOWING)
      .not.toBe(0);
    expect(permission.compareDocumentPosition(work) & Node.DOCUMENT_POSITION_FOLLOWING).not.toBe(0);
    expect(work.compareDocumentPosition(plan) & Node.DOCUMENT_POSITION_FOLLOWING).not.toBe(0);
    expect(within(permission).getByRole("status", { name: "Permission resolved" }))
      .toHaveTextContent("Allowed once");
    expect(within(permission).queryByRole("button")).not.toBeInTheDocument();
  });

  it("routes exact permission and plan answers from the active anchored card", async () => {
    const user = userEvent.setup();
    const onSessionEvent = vi.fn();
    const permissionState = transcriptReducer(
      transcriptReducer(emptyTranscript(), event("turn_started", { sequence: 1 })),
      event("permission_requested", {
        sequence: 2,
        request_id: "permission-route",
        action: "bash",
        scope: '{"command":"npm test"}',
      }),
    );
    const { rerender } = render(
      <Conversation
        state={permissionState}
        openInspector={() => {}}
        onSessionEvent={onSessionEvent}
      />,
    );
    await user.click(screen.getByRole("button", { name: /^Always for this session/ }));
    expect(onSessionEvent).toHaveBeenCalledWith({
      type: "answer_permission",
      request_id: "permission-route",
      answer: "always",
    });

    const planState = transcriptReducer(
      transcriptReducer(emptyTranscript(), event("turn_started", { sequence: 1 })),
      event("plan_approval_requested", {
        sequence: 2,
        request_id: "plan-route",
        plan: "1. Run checks",
      }),
    );
    rerender(
      <Conversation state={planState} openInspector={() => {}} onSessionEvent={onSessionEvent} />,
    );
    await user.click(screen.getByRole("button", { name: "Approve plan" }));
    expect(onSessionEvent).toHaveBeenLastCalledWith({
      type: "answer_plan",
      request_id: "plan-route",
      approved: true,
    });
  });

  it("renders a terminal unresolved permission honestly until a late resolution arrives", () => {
    let state = transcriptReducer(emptyTranscript(), event("turn_started", { sequence: 1 }));
    state = transcriptReducer(state, event("permission_requested", {
      sequence: 2,
      request_id: "late-permission",
      action: "bash",
      scope: '{"command":"npm test"}',
    }));
    state = transcriptReducer(state, event("turn_cancelled", { sequence: 3 }));
    const { rerender } = render(
      <Conversation state={state} openInspector={() => {}} onSessionEvent={() => {}} />,
    );

    expect(screen.getByRole("status", { name: "Permission unresolved" })).toHaveTextContent(
      "Turn ended without a recorded decision.",
    );
    expect(screen.queryByText("Awaiting an authoritative decision")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Allow once/ })).not.toBeInTheDocument();

    state = transcriptReducer(state, event("permission_resolved", {
      sequence: 4,
      request_id: "late-permission",
      answer: "no",
    }));
    rerender(<Conversation state={state} openInspector={() => {}} onSessionEvent={() => {}} />);
    expect(screen.getByRole("status", { name: "Permission resolved" })).toHaveTextContent("Denied");
    expect(screen.queryByRole("status", { name: "Permission unresolved" })).not.toBeInTheDocument();
  });

  it("renders a terminal unresolved plan honestly until a late resolution arrives", () => {
    let state = transcriptReducer(emptyTranscript(), event("turn_started", { sequence: 1 }));
    state = transcriptReducer(state, event("plan_approval_requested", {
      sequence: 2,
      request_id: "late-plan",
      plan: "1. Verify the change",
    }));
    state = transcriptReducer(state, event("turn_failed", {
      sequence: 3,
      error_category: "provider",
      message: "Turn stopped",
    }));
    const { rerender } = render(
      <Conversation state={state} openInspector={() => {}} onSessionEvent={() => {}} />,
    );

    expect(screen.getByRole("status", { name: "Plan review unresolved" })).toHaveTextContent(
      "Turn ended without a recorded decision.",
    );
    expect(screen.queryByText("Awaiting an authoritative decision")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Approve plan" })).not.toBeInTheDocument();

    state = transcriptReducer(state, event("plan_approval_resolved", {
      sequence: 4,
      request_id: "late-plan",
      approved: false,
      feedback: "Add verification",
    }));
    rerender(<Conversation state={state} openInspector={() => {}} onSessionEvent={() => {}} />);
    expect(screen.getByRole("status", { name: "Plan review resolved" })).toHaveTextContent(
      "Revision requested",
    );
    expect(screen.queryByRole("status", { name: "Plan review unresolved" })).not.toBeInTheDocument();
  });

  it("does not render or position visually empty structured authoritative messages", () => {
    let state = transcriptReducer(emptyTranscript(), event("turn_started", { sequence: 1 }));
    state = transcriptReducer(state, event("assistant_delta", { sequence: 2, text: "Draft before" }));
    state = transcriptReducer(
      state,
      event("activity_started", { sequence: 3, activity_id: "read-current", name: "read_file" }),
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
    render(<Conversation state={state} openInspector={() => {}} />);

    const before = screen.getByText("Authoritative before").closest("article");
    const work = screen.getByRole("button", { name: /Open activity/ });
    const after = screen.getByText("Authoritative after").closest("article");
    expect(screen.getAllByRole("article", { name: "Assistant message" })).toHaveLength(2);
    expect(screen.getAllByText("Authoritative before")).toHaveLength(1);
    expect(screen.getAllByText("Authoritative after")).toHaveLength(1);
    expect(screen.queryByText("Draft before")).not.toBeInTheDocument();
    expect(screen.queryByText("Draft after")).not.toBeInTheDocument();
    expect((before?.compareDocumentPosition(work) ?? 0) & Node.DOCUMENT_POSITION_FOLLOWING).not.toBe(0);
    expect(work.compareDocumentPosition(after as Node) & Node.DOCUMENT_POSITION_FOLLOWING).not.toBe(0);
  });

  it("auto-scrolls only from near the bottom and otherwise offers New messages", async () => {
    const user = userEvent.setup();
    const initial = transcript({ messages: messages({ role: "assistant", content: "First" }) });
    const { rerender } = render(<Conversation state={initial} openInspector={() => {}} />);
    const scroller = screen.getByRole("log", { name: "Conversation transcript" });
    Object.defineProperties(scroller, {
      clientHeight: { configurable: true, value: 200 },
      scrollHeight: { configurable: true, value: 1_000 },
    });

    scroller.scrollTop = 760;
    fireEvent.scroll(scroller);
    rerender(
      <Conversation
        state={transcript({
          lastSequence: 2,
          messages: messages(
            { role: "assistant", content: "First" },
            { role: "assistant", content: "Second" },
          ),
        })}
        openInspector={() => {}}
      />,
    );
    expect(scroller.scrollTop).toBe(1_000);
    expect(screen.queryByRole("button", { name: "New messages" })).not.toBeInTheDocument();

    scroller.scrollTop = 200;
    fireEvent.scroll(scroller);
    rerender(
      <Conversation
        state={transcript({
          lastSequence: 3,
          messages: messages(
            { role: "assistant", content: "First" },
            { role: "assistant", content: "Second" },
            { role: "assistant", content: "Third" },
          ),
        })}
        openInspector={() => {}}
      />,
    );
    expect(scroller.scrollTop).toBe(200);
    await user.click(screen.getByRole("button", { name: "New messages" }));
    expect(scroller.scrollTop).toBe(1_000);
    expect(screen.queryByRole("button", { name: "New messages" })).not.toBeInTheDocument();
  });

  it("renders a compact grouped activity summary and opens the inspector by activity id", async () => {
    const user = userEvent.setup();
    const openInspector = vi.fn();
    const first = activity();
    const second = activity({
      activityId: "activity-2",
      name: "read_file",
      durationMs: 500,
      result: { path: "/work/long-result.ts", output: "x".repeat(400) },
    });
    render(
      <Conversation
        state={transcript({
          activities: { [first.activityId]: first, [second.activityId]: second },
          activityOrder: [first.activityId, second.activityId],
        })}
        openInspector={openInspector}
      />,
    );

    const card = screen.getByRole("button", { name: /Open activity/ });
    expect(card).toHaveAccessibleName(/Complete.*34\.5s.*2 actions.*18 tests passed/);
    expect(card).toHaveTextContent("Complete");
    expect(card).toHaveTextContent("34.5s");
    expect(card).toHaveTextContent("2 actions");
    expect(card).toHaveTextContent("18 tests passed");
    expect(card).toHaveTextContent("x".repeat(400));
    expect(within(card).getByText(/x{100}/)).toHaveClass(/preview/);
    await user.click(card);
    expect(openInspector).toHaveBeenCalledWith("activity-1");
  });

  it("updates running activity elapsed time and cleans up its timer", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-08-08T00:00:05Z"));
    const running = activity({
      status: "running",
      startedAt: "2026-08-08T00:00:00Z",
      result: undefined,
      isError: undefined,
      durationMs: undefined,
    });
    const { unmount } = render(
      <Conversation
        state={transcript({
          activities: { [running.activityId]: running },
          activityOrder: [running.activityId],
        })}
        openInspector={() => {}}
      />,
    );

    expect(screen.getByRole("button", { name: /Open activity/ })).toHaveTextContent("5s");
    expect(vi.getTimerCount()).toBe(1);
    act(() => vi.advanceTimersByTime(1_000));
    expect(screen.getByRole("button", { name: /Open activity/ })).toHaveTextContent("6s");
    unmount();
    expect(vi.getTimerCount()).toBe(0);
  });

  it("shows a legitimate completed zero duration", () => {
    const completed = activity({ durationMs: 0 });
    render(
      <Conversation
        state={transcript({
          activities: { [completed.activityId]: completed },
          activityOrder: [completed.activityId],
        })}
        openInspector={() => {}}
      />,
    );

    expect(screen.getByRole("button", { name: /Open activity/ })).toHaveTextContent("0ms");
  });
});
