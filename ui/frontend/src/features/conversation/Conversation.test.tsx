import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { ActivityItem, HarnessMessage, TranscriptState } from "../../protocol/types";
import { Conversation } from "./Conversation";

afterEach(cleanup);

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
    permission: null,
    planReview: null,
    running: false,
    stopping: false,
    queued: null,
    safety: null,
    error: null,
    ...overrides,
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
        })}
        openInspector={() => {}}
      />,
    );

    expect(screen.queryByText("Draft answer")).not.toBeInTheDocument();
    expect(screen.getByText("Authoritative answer")).toBeVisible();
    expect(screen.getByRole("status", { name: "Conversation update" })).toHaveTextContent("Response complete");
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
});
