import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { ClientEvent, QueuedMessage } from "../../protocol/types";
import { Composer } from "./Composer";

type StorageDouble = Pick<Storage, "getItem" | "setItem">;

function storage(initial: Record<string, string> = {}) {
  const values = new Map(Object.entries(initial));
  const target: StorageDouble = {
    getItem: (key) => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, value),
  };
  return { target, values };
}

function renderComposer(options: {
  sessionId?: string;
  running?: boolean;
  stopping?: boolean;
  queued?: QueuedMessage | null;
  onEvent?: (event: ClientEvent) => void | Promise<void>;
  onStop?: () => void | Promise<void>;
  draftStorage?: StorageDouble;
  backupDelayMs?: number;
  draftMemory?: Map<string, string>;
} = {}) {
  return render(
    <Composer
      sessionId={options.sessionId ?? "session-a"}
      running={options.running ?? false}
      stopping={options.stopping ?? false}
      queued={options.queued ?? null}
      onEvent={options.onEvent ?? (() => {})}
      onStop={options.onStop ?? (() => {})}
      draftStorage={options.draftStorage ?? storage().target}
      backupDelayMs={options.backupDelayMs}
      draftMemory={options.draftMemory ?? new Map()}
    />,
  );
}

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe("Composer", () => {
  it("sends a non-blank idle draft in base mode and clears it after success", async () => {
    const user = userEvent.setup();
    const onEvent = vi.fn();
    renderComposer({ onEvent });

    const textbox = screen.getByRole("textbox", { name: "Message" });
    await user.type(textbox, "Inspect the reducer");
    await user.click(screen.getByRole("button", { name: "Send message" }));

    expect(onEvent).toHaveBeenCalledWith({
      type: "send_message",
      text: "Inspect the reducer",
      mode: "base",
    });
    await waitFor(() => expect(textbox).toHaveValue(""));
  });

  it("keeps the composer editable and queues one follow-up while running", async () => {
    const user = userEvent.setup();
    const onEvent = vi.fn();
    renderComposer({ running: true, onEvent });

    const textbox = screen.getByRole("textbox", { name: "Message" });
    expect(textbox).toBeEnabled();
    await user.type(textbox, "next request");
    await user.keyboard("{Meta>}{Enter}{/Meta}");

    expect(onEvent).toHaveBeenCalledWith({
      type: "queue_message",
      text: "next request",
      mode: "base",
    });
    await waitFor(() => expect(textbox).toHaveValue(""));
  });

  it("updates the existing queued follow-up instead of creating another kind of event", async () => {
    const user = userEvent.setup();
    const onEvent = vi.fn();
    renderComposer({
      running: true,
      queued: { type: "queue_message", text: "original follow-up", mode: "base" },
      onEvent,
    });

    expect(screen.getByRole("status", { name: "Queued follow-up" })).toHaveTextContent(
      "original follow-up",
    );
    await user.type(screen.getByRole("textbox", { name: "Message" }), "replacement follow-up");
    await user.click(screen.getByRole("button", { name: "Update queued message" }));

    expect(onEvent).toHaveBeenCalledWith({
      type: "queue_message",
      text: "replacement follow-up",
      mode: "base",
    });
  });

  it("clears a queued follow-up and returns its text and mode to the editable draft", async () => {
    const user = userEvent.setup();
    const onEvent = vi.fn();
    renderComposer({
      running: true,
      queued: { type: "queue_message", text: "keep this request", mode: "plan" },
      onEvent,
    });

    await user.click(screen.getByRole("button", { name: "Edit queued follow-up" }));

    expect(onEvent).toHaveBeenCalledWith({ type: "clear_queued_message" });
    await waitFor(() => {
      expect(screen.getByRole("textbox", { name: "Message" })).toHaveValue("keep this request");
      expect(screen.getByRole("button", { name: "Plan mode" })).toHaveAttribute(
        "aria-pressed",
        "true",
      );
    });
  });

  it("does not submit blank input", async () => {
    const user = userEvent.setup();
    const onEvent = vi.fn();
    renderComposer({ onEvent });

    const textbox = screen.getByRole("textbox", { name: "Message" });
    await user.type(textbox, "   ");
    expect(screen.getByRole("button", { name: "Send message" })).toBeDisabled();
    await user.keyboard("{Meta>}{Enter}{/Meta}");
    expect(onEvent).not.toHaveBeenCalled();
    expect(textbox).toHaveValue("   ");
  });

  it("uses Command+Enter but leaves an ordinary Enter for multiline input", async () => {
    const user = userEvent.setup();
    const onEvent = vi.fn();
    renderComposer({ onEvent });

    const textbox = screen.getByRole("textbox", { name: "Message" });
    await user.type(textbox, "line one{Enter}line two");
    expect(onEvent).not.toHaveBeenCalled();
    expect(textbox).toHaveValue("line one\nline two");
    await user.keyboard("{Meta>}{Enter}{/Meta}");
    expect(onEvent).toHaveBeenCalledTimes(1);
  });

  it("does not submit during IME composition", () => {
    const onEvent = vi.fn();
    renderComposer({ onEvent });
    const textbox = screen.getByRole("textbox", { name: "Message" });

    fireEvent.compositionStart(textbox);
    fireEvent.change(textbox, { target: { value: "計画" } });
    fireEvent.keyDown(textbox, { key: "Enter", metaKey: true, isComposing: true });
    expect(onEvent).not.toHaveBeenCalled();
    expect(textbox).toHaveValue("計画");

    fireEvent.compositionEnd(textbox);
    fireEvent.keyDown(textbox, { key: "Enter", metaKey: true });
    expect(onEvent).toHaveBeenCalledWith({ type: "send_message", text: "計画", mode: "base" });
  });

  it("preserves failed text and retries the exact event", async () => {
    const user = userEvent.setup();
    let attempts = 0;
    const onEvent = vi.fn(async () => {
      attempts += 1;
      if (attempts === 1) throw new Error("offline");
    });
    renderComposer({ onEvent });

    const textbox = screen.getByRole("textbox", { name: "Message" });
    await user.type(textbox, "do not lose this");
    await user.click(screen.getByRole("button", { name: "Send message" }));

    expect(await screen.findByRole("status", { name: "Message status" })).toHaveTextContent(
      "Message not sent",
    );
    expect(textbox).toHaveValue("do not lose this");
    await user.click(screen.getByRole("button", { name: "Retry message" }));
    await waitFor(() => expect(textbox).toHaveValue(""));
    expect(onEvent).toHaveBeenNthCalledWith(1, {
      type: "send_message",
      text: "do not lose this",
      mode: "base",
    });
    expect(onEvent).toHaveBeenNthCalledWith(2, {
      type: "send_message",
      text: "do not lose this",
      mode: "base",
    });
  });

  it("applies base or plan per turn without emitting a session-mode event", async () => {
    const user = userEvent.setup();
    const events: ClientEvent[] = [];
    renderComposer({ onEvent: (event) => {
      events.push(event);
    } });

    await user.click(screen.getByRole("button", { name: "Plan mode" }));
    await user.type(screen.getByRole("textbox", { name: "Message" }), "make a plan");
    await user.keyboard("{Meta>}{Enter}{/Meta}");

    expect(events).toEqual([{ type: "send_message", text: "make a plan", mode: "plan" }]);
    expect(events.some((event) => event.type === "set_session_mode")).toBe(false);
  });

  it("offers slash suggestions and supports keyboard selection", async () => {
    const user = userEvent.setup();
    renderComposer();
    const textbox = screen.getByRole("textbox", { name: "Message" });

    await user.type(textbox, "/pl");
    const suggestions = screen.getByRole("listbox", { name: "Composer commands" });
    expect(suggestions).toBeVisible();
    expect(screen.getByRole("option", { name: /Plan mode/ })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    await user.keyboard("{ArrowDown}{ArrowUp}{Enter}");

    expect(screen.queryByRole("listbox", { name: "Composer commands" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Plan mode" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    expect(textbox).toHaveValue("");
    expect(textbox).toHaveFocus();
  });

  it("closes suggestions on the first Escape and focuses Stop on the second", async () => {
    const user = userEvent.setup();
    renderComposer({ running: true });
    const textbox = screen.getByRole("textbox", { name: "Message" });

    await user.type(textbox, "/");
    expect(screen.getByRole("listbox", { name: "Composer commands" })).toBeVisible();
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("listbox", { name: "Composer commands" })).not.toBeInTheDocument();
    expect(textbox).toHaveFocus();
    await user.click(screen.getByRole("button", { name: "Plan mode" }));
    await user.keyboard("{Escape}");
    expect(screen.getByRole("button", { name: "Stop turn" })).toHaveFocus();
  });

  it("renders a square Stop control and announces the stopping state", async () => {
    const user = userEvent.setup();
    const onStop = vi.fn();
    const draftBackup = storage().target;
    const { rerender } = render(
      <Composer
        sessionId="stop-session"
        running
        stopping={false}
        queued={null}
        onEvent={() => {}}
        onStop={onStop}
        draftStorage={draftBackup}
      />,
    );

    const stop = screen.getByRole("button", { name: "Stop turn" });
    expect(stop).toHaveStyle({ aspectRatio: "1" });
    await user.click(stop);
    expect(onStop).toHaveBeenCalledTimes(1);

    rerender(
      <Composer
        sessionId="stop-session"
        running
        stopping
        queued={null}
        onEvent={() => {}}
        onStop={onStop}
        draftStorage={draftBackup}
      />,
    );
    expect(screen.getByRole("button", { name: "Stopping turn" })).toBeDisabled();
    expect(screen.getByRole("status", { name: "Turn status" })).toHaveTextContent(
      "Stopping after current action",
    );
  });

  it("isolates drafts by stable session id and restores each one when switching", async () => {
    const user = userEvent.setup();
    const memory = new Map<string, string>();
    const draftBackup = storage().target;
    const { rerender } = renderComposer({
      sessionId: "session-one",
      draftMemory: memory,
      draftStorage: draftBackup,
    });
    const textbox = screen.getByRole("textbox", { name: "Message" });
    await user.type(textbox, "first draft");

    rerender(
      <Composer
        sessionId="session-two"
        running={false}
        stopping={false}
        queued={null}
        onEvent={() => {}}
        onStop={() => {}}
        draftMemory={memory}
        draftStorage={draftBackup}
      />,
    );
    expect(textbox).toHaveValue("");
    await user.type(textbox, "second draft");

    rerender(
      <Composer
        sessionId="session-one"
        running={false}
        stopping={false}
        queued={null}
        onEvent={() => {}}
        onStop={() => {}}
        draftMemory={memory}
        draftStorage={draftBackup}
      />,
    );
    expect(textbox).toHaveValue("first draft");
  });

  it("debounces a text-only backup and restores it in a fresh composer", async () => {
    vi.useFakeTimers();
    const backup = storage();
    const { unmount } = renderComposer({
      sessionId: "persisted-session",
      draftStorage: backup.target,
      backupDelayMs: 250,
      draftMemory: new Map(),
    });
    fireEvent.change(screen.getByRole("textbox", { name: "Message" }), {
      target: { value: "backup text" },
    });

    expect(backup.values.size).toBe(0);
    act(() => vi.advanceTimersByTime(249));
    expect(backup.values.size).toBe(0);
    act(() => vi.advanceTimersByTime(1));
    expect([...backup.values.values()]).toEqual([JSON.stringify({ text: "backup text" })]);

    unmount();
    renderComposer({
      sessionId: "persisted-session",
      draftStorage: backup.target,
      backupDelayMs: 250,
      draftMemory: new Map(),
    });
    expect(screen.getByRole("textbox", { name: "Message" })).toHaveValue("backup text");
  });

  it("focuses the editable draft when a running turn completes", () => {
    const draftBackup = storage().target;
    const { rerender } = render(
      <Composer
        sessionId="completion-session"
        running
        stopping={false}
        queued={null}
        onEvent={() => {}}
        onStop={() => {}}
        draftStorage={draftBackup}
      />,
    );
    const textbox = screen.getByRole("textbox", { name: "Message" });
    textbox.blur();

    rerender(
      <Composer
        sessionId="completion-session"
        running={false}
        stopping={false}
        queued={null}
        onEvent={() => {}}
        onStop={() => {}}
        draftStorage={draftBackup}
      />,
    );
    expect(textbox).toHaveFocus();
  });

  it("keeps the unavailable attachment action visibly honest", () => {
    renderComposer();
    expect(screen.getByRole("button", { name: "Attach context (coming later)" })).toBeDisabled();
  });
});
