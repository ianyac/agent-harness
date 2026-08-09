import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it } from "vitest";

import type { HarnessMessage, TranscriptState } from "../../protocol/types";
import { Conversation } from "./Conversation";

afterEach(cleanup);

function state(messages: HarnessMessage[]): TranscriptState {
  return {
    generation: 1,
    lastSequence: 1,
    messages,
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
  };
}

describe("Conversation search", () => {
  it("opens with Command+F, moves through literal matches, and never rewrites message content", async () => {
    const user = userEvent.setup();
    render(
      <Conversation
        state={state([
          { role: "user", content: "Alpha starts here, then alpha repeats." },
          { role: "assistant", content: "The final alpha is here." },
        ])}
        openInspector={() => {}}
      />,
    );
    const firstMessage = screen.getByRole("article", { name: "User message" });
    const originalMarkup = firstMessage.innerHTML;

    await user.keyboard("{Meta>}f{/Meta}");
    const search = screen.getByRole("searchbox", { name: "Search conversation" });
    expect(search).toHaveFocus();
    await user.type(search, "alpha");

    expect(screen.getByRole("status", { name: "Search result position" })).toHaveTextContent("1 of 3");
    expect(firstMessage.innerHTML).toBe(originalMarkup);
    expect(firstMessage.querySelector("mark")).toBeNull();
    await user.click(screen.getByRole("button", { name: "Next match" }));
    expect(screen.getByRole("status", { name: "Search result position" })).toHaveTextContent("2 of 3");
    await user.click(screen.getByRole("button", { name: "Next match" }));
    expect(screen.getByRole("status", { name: "Search result position" })).toHaveTextContent("3 of 3");
  });

  it("returns focus to the matched message when search closes", async () => {
    const user = userEvent.setup();
    render(
      <Conversation
        state={state([
          { role: "user", content: "First needle" },
          { role: "assistant", content: "Second needle" },
        ])}
        openInspector={() => {}}
      />,
    );

    await user.keyboard("{Meta>}f{/Meta}");
    await user.type(screen.getByRole("searchbox", { name: "Search conversation" }), "needle");
    await user.click(screen.getByRole("button", { name: "Next match" }));
    await user.click(screen.getByRole("button", { name: "Close conversation search" }));

    expect(screen.queryByRole("searchbox", { name: "Search conversation" })).not.toBeInTheDocument();
    expect(screen.getByRole("article", { name: "Assistant message" })).toHaveFocus();
  });

  it("supports Enter, Shift+Enter, and Escape while keeping result controls named", async () => {
    const user = userEvent.setup();
    render(
      <Conversation
        state={state([
          { role: "user", content: "One match" },
          { role: "assistant", content: "Another match" },
        ])}
        openInspector={() => {}}
      />,
    );

    await user.keyboard("{Meta>}f{/Meta}");
    const search = screen.getByRole("searchbox", { name: "Search conversation" });
    await user.type(search, "match");
    await user.keyboard("{Enter}");
    expect(screen.getByRole("status", { name: "Search result position" })).toHaveTextContent("2 of 2");
    await user.keyboard("{Shift>}{Enter}{/Shift}");
    expect(screen.getByRole("status", { name: "Search result position" })).toHaveTextContent("1 of 2");
    expect(within(screen.getByRole("search", { name: "Conversation search" })).getAllByRole("button")).toHaveLength(3);
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("search", { name: "Conversation search" })).not.toBeInTheDocument();
    expect(screen.getByRole("article", { name: "User message" })).toHaveFocus();
  });
});
