import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import "../../styles/tokens.css";
import "../../styles/global.css";
import type { HarnessMessage, TranscriptState } from "../../protocol/types";
import { Conversation } from "./Conversation";
import { ConversationSearch } from "./ConversationSearch";

afterEach(cleanup);

function state(messages: HarnessMessage[]): TranscriptState {
  return {
    generation: 1,
    lastSequence: 1,
    messages,
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
    latestContext: null,
    error: null,
    terminal: null,
    activeTurn: null,
  };
}

describe("Conversation search", () => {
  it("opens with Command+F, moves through literal matches, and never rewrites message content", async () => {
    const user = userEvent.setup();
    render(
      <Conversation
        ownsSearchShortcut
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
        ownsSearchShortcut
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
        ownsSearchShortcut
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

  it("scrolls each active match into view while keeping the search input focused", async () => {
    const user = userEvent.setup();
    render(
      <Conversation
        ownsSearchShortcut
        state={state([
          { role: "user", content: "First needle" },
          { role: "assistant", content: "Second needle" },
        ])}
        openInspector={() => {}}
      />,
    );
    const userMessage = screen.getByRole("article", { name: "User message" });
    const assistantMessage = screen.getByRole("article", { name: "Assistant message" });
    const scrollUser = vi.fn();
    const scrollAssistant = vi.fn();
    userMessage.scrollIntoView = scrollUser;
    assistantMessage.scrollIntoView = scrollAssistant;

    await user.keyboard("{Meta>}f{/Meta}");
    const search = screen.getByRole("searchbox", { name: "Search conversation" });
    await user.type(search, "needle");
    expect(scrollUser).toHaveBeenLastCalledWith({ block: "center", behavior: "auto" });
    expect(search).toHaveFocus();

    await user.click(screen.getByRole("button", { name: "Next match" }));
    expect(scrollAssistant).toHaveBeenLastCalledWith({ block: "center", behavior: "auto" });
    expect(search).toHaveFocus();
    await user.click(screen.getByRole("button", { name: "Previous match" }));
    expect(scrollUser).toHaveBeenCalledTimes(2);
    expect(search).toHaveFocus();
  });

  it("counts the literal query including leading and trailing spaces", async () => {
    const user = userEvent.setup();
    render(
      <Conversation
        ownsSearchShortcut
        state={state([
          { role: "user", content: "Keep-alpha-without padding." },
          { role: "assistant", content: "Only this has one exact  alpha  occurrence." },
        ])}
        openInspector={() => {}}
      />,
    );

    await user.keyboard("{Meta>}f{/Meta}");
    await user.type(screen.getByRole("searchbox", { name: "Search conversation" }), " alpha ");

    expect(screen.getByRole("status", { name: "Search result position" })).toHaveTextContent("1 of 1");
  });

  it("keeps a visible focus ring on the keyboard-focused search box", async () => {
    const user = userEvent.setup();
    render(
      <Conversation
        ownsSearchShortcut
        state={state([{ role: "assistant", content: "Searchable" }])}
        openInspector={() => {}}
      />,
    );

    await user.keyboard("{Meta>}f{/Meta}");
    const search = screen.getByRole("searchbox", { name: "Search conversation" });
    expect(search).toHaveFocus();
    expect(getComputedStyle(search).outlineStyle).toBe("solid");
  });

  it("closes with Escape from anywhere inside the overlay", async () => {
    const user = userEvent.setup();
    const onClose = vi.fn();
    render(
      <ConversationSearch
        messages={[{ id: "message-1", text: "needle" }]}
        onClose={onClose}
      />,
    );

    await user.type(screen.getByRole("searchbox", { name: "Search conversation" }), "needle");
    screen.getByRole("button", { name: "Next match" }).focus();
    await user.keyboard("{Escape}");

    expect(onClose).toHaveBeenCalledWith("message-1");
  });

  it("does not intercept Command+F when this instance does not own the shortcut", () => {
    render(
      <>
        <label>
          Draft
          <textarea defaultValue="keep typing" />
        </label>
        <Conversation state={state([])} openInspector={() => {}} />
      </>,
    );
    const draft = screen.getByRole("textbox", { name: "Draft" });
    draft.focus();
    const shortcut = new KeyboardEvent("keydown", {
      key: "f",
      metaKey: true,
      bubbles: true,
      cancelable: true,
    });

    window.dispatchEvent(shortcut);

    expect(shortcut.defaultPrevented).toBe(false);
    expect(draft).toHaveFocus();
    expect(screen.queryByRole("searchbox", { name: "Search conversation" })).not.toBeInTheDocument();
  });
});
