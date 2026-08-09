import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { ClientEvent, QueuedMessage } from "../../protocol/types";
import tokensCss from "../../styles/tokens.css?raw";
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

function deferred<T = void>() {
  let resolve!: (value: T | PromiseLike<T>) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((accept, decline) => {
    resolve = accept;
    reject = decline;
  });
  return { promise, resolve, reject };
}

function cssTokenValues(token: string) {
  const prefix = `${token}:`;
  return tokensCss.split("\n")
    .map((line) => line.trim())
    .filter((line) => line.startsWith(prefix))
    .map((line) => line.slice(prefix.length, -1).trim());
}

function relativeLuminance(hex: string) {
  const channels = hex.match(/[a-f\d]{2}/gi)?.map((channel) => Number.parseInt(channel, 16) / 255);
  if (channels === undefined) throw new Error(`Expected a six-digit hex color, received ${hex}`);
  const [red, green, blue] = channels.map((channel) =>
    channel <= 0.04045 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4);
  return 0.2126 * red + 0.7152 * green + 0.0722 * blue;
}

function contrastRatio(foreground: string, background: string) {
  const foregroundLuminance = relativeLuminance(foreground);
  const backgroundLuminance = relativeLuminance(background);
  return (Math.max(foregroundLuminance, backgroundLuminance) + 0.05) /
    (Math.min(foregroundLuminance, backgroundLuminance) + 0.05);
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
    expect(screen.getByRole("textbox", { name: "Message" })).toHaveFocus();
  });

  it("offers a distinct clear control that discards the queued follow-up without editing it", async () => {
    const user = userEvent.setup();
    const onEvent = vi.fn();
    renderComposer({
      running: true,
      queued: { type: "queue_message", text: "discard this request", mode: "plan" },
      onEvent,
    });

    expect(screen.getByRole("button", { name: "Edit queued follow-up" })).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Clear queued follow-up" }));

    expect(onEvent).toHaveBeenCalledWith({ type: "clear_queued_message" });
    await waitFor(() => expect(screen.getByRole("textbox", { name: "Message" })).toHaveValue(""));
    expect(screen.getByRole("button", { name: "Base mode" })).toHaveAttribute("aria-pressed", "true");
  });

  it("preserves edits made while queue clear is pending and offers the cleared text non-destructively", async () => {
    const user = userEvent.setup();
    const clear = deferred();
    renderComposer({
      running: true,
      queued: { type: "queue_message", text: "queued plan", mode: "plan" },
      onEvent: (event) => event.type === "clear_queued_message" ? clear.promise : undefined,
    });

    await user.click(screen.getByRole("button", { name: "Edit queued follow-up" }));
    const textbox = screen.getByRole("textbox", { name: "Message" });
    await user.type(textbox, "new work");
    await user.click(screen.getByRole("button", { name: "Plan mode" }));
    await user.click(screen.getByRole("button", { name: "Base mode" }));
    await act(async () => {
      clear.resolve();
      await clear.promise;
    });

    expect(textbox).toHaveValue("new work");
    expect(screen.getByRole("button", { name: "Base mode" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    const reconciliation = screen.getByRole("status", { name: "Queue reconciliation" });
    expect(reconciliation).toHaveTextContent("queued plan");
    await user.click(screen.getByRole("button", { name: "Append cleared follow-up" }));
    expect(textbox).toHaveValue("new work\nqueued plan");
    expect(screen.getByRole("button", { name: "Base mode" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
  });

  it("keeps a delayed clear completion with its originating session", async () => {
    const user = userEvent.setup();
    const clear = deferred();
    const memory = new Map<string, string>();
    const draftBackup = storage().target;
    const onEvent = (event: ClientEvent) =>
      event.type === "clear_queued_message" ? clear.promise : undefined;
    const { rerender } = renderComposer({
      sessionId: "session-one",
      running: true,
      queued: { type: "queue_message", text: "session one queue", mode: "plan" },
      onEvent,
      draftMemory: memory,
      draftStorage: draftBackup,
    });

    await user.click(screen.getByRole("button", { name: "Edit queued follow-up" }));
    rerender(
      <Composer
        sessionId="session-two"
        running={false}
        stopping={false}
        queued={null}
        onEvent={onEvent}
        onStop={() => {}}
        draftMemory={memory}
        draftStorage={draftBackup}
      />,
    );
    const textbox = screen.getByRole("textbox", { name: "Message" });
    await user.type(textbox, "session two draft");
    await act(async () => {
      clear.resolve();
      await clear.promise;
    });

    expect(textbox).toHaveValue("session two draft");
    expect(screen.queryByRole("status", { name: "Queue reconciliation" })).not.toBeInTheDocument();

    rerender(
      <Composer
        sessionId="session-one"
        running={false}
        stopping={false}
        queued={null}
        onEvent={onEvent}
        onStop={() => {}}
        draftMemory={memory}
        draftStorage={draftBackup}
      />,
    );
    expect(textbox).toHaveValue("session one queue");
    expect(screen.getByRole("button", { name: "Plan mode" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    expect(screen.queryByRole("status", { name: "Queue reconciliation" })).not.toBeInTheDocument();
  });

  it("debounces an inactive session's restored queue into its text-only backup", async () => {
    vi.useFakeTimers();
    const clear = deferred();
    const sessionId = "clear/session-a";
    const storageKey = "agent-harness:draft:clear%2Fsession-a";
    const backup = storage({
      [storageKey]: JSON.stringify({ text: "" }),
    });
    const memory = new Map<string, string>();
    const onEvent = (event: ClientEvent) =>
      event.type === "clear_queued_message" ? clear.promise : undefined;
    const { rerender, unmount } = renderComposer({
      sessionId,
      running: true,
      queued: { type: "queue_message", text: "restored while inactive", mode: "plan" },
      onEvent,
      draftMemory: memory,
      draftStorage: backup.target,
      backupDelayMs: 250,
    });

    fireEvent.click(screen.getByRole("button", { name: "Edit queued follow-up" }));
    rerender(
      <Composer
        sessionId="session-b"
        running={false}
        stopping={false}
        queued={null}
        onEvent={onEvent}
        onStop={() => {}}
        draftMemory={memory}
        draftStorage={backup.target}
        backupDelayMs={250}
      />,
    );
    await act(async () => {
      clear.resolve();
      await clear.promise;
    });

    expect(backup.values.get(storageKey)).toBe(JSON.stringify({ text: "" }));
    act(() => vi.advanceTimersByTime(249));
    expect(backup.values.get(storageKey)).toBe(JSON.stringify({ text: "" }));
    act(() => vi.advanceTimersByTime(1));
    unmount();

    renderComposer({
      sessionId,
      draftStorage: backup.target,
      backupDelayMs: 250,
      draftMemory: new Map(),
    });
    expect(screen.getByRole("textbox", { name: "Message" })).toHaveValue(
      "restored while inactive",
    );
  });

  it("keeps a rejected queue clear retryable without changing the draft", async () => {
    const user = userEvent.setup();
    const first = deferred();
    const second = deferred();
    let attempt = 0;
    const onEvent = (event: ClientEvent) => {
      if (event.type !== "clear_queued_message") return;
      attempt += 1;
      return attempt === 1 ? first.promise : second.promise;
    };
    renderComposer({
      running: true,
      queued: { type: "queue_message", text: "retry this queue", mode: "plan" },
      onEvent,
    });

    await user.click(screen.getByRole("button", { name: "Edit queued follow-up" }));
    await act(async () => {
      first.reject(new Error("offline"));
      await first.promise.catch(() => {});
    });

    expect(screen.getByRole("textbox", { name: "Message" })).toHaveValue("");
    expect(screen.getByRole("status", { name: "Queue reconciliation" })).toHaveTextContent(
      "Queued follow-up was not cleared",
    );
    await user.click(screen.getByRole("button", { name: "Retry clearing follow-up" }));
    await act(async () => {
      second.resolve();
      await second.promise;
    });
    expect(screen.getByRole("textbox", { name: "Message" })).toHaveValue("retry this queue");
    expect(screen.getByRole("button", { name: "Plan mode" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
  });

  it.each([
    {
      changedField: "text",
      replacement: {
        type: "queue_message" as const,
        text: "replacement queue B",
        mode: "plan" as const,
      },
      visibleReplacement: "replacement queue B",
    },
    {
      changedField: "mode",
      replacement: {
        type: "queue_message" as const,
        text: "original queue A",
        mode: "base" as const,
      },
      visibleReplacement: "Queued · Base",
    },
    {
      changedField: "presence",
      replacement: null,
      visibleReplacement: null,
    },
  ])("does not retry a failed clear when controlled queue $changedField changes", async ({
    replacement,
    visibleReplacement,
  }) => {
    const user = userEvent.setup();
    const first = deferred();
    const queueA: QueuedMessage = {
      type: "queue_message",
      text: "original queue A",
      mode: "plan",
    };
    const onEvent = vi.fn((event: ClientEvent) =>
      event.type === "clear_queued_message" ? first.promise : undefined);
    const draftMemory = new Map<string, string>();
    const draftStorage = storage().target;
    const { rerender } = renderComposer({
      running: true,
      queued: queueA,
      onEvent,
      draftMemory,
      draftStorage,
    });

    await user.click(screen.getByRole("button", { name: "Edit queued follow-up" }));
    await act(async () => {
      first.reject(new Error("offline"));
      await first.promise.catch(() => {});
    });
    rerender(
      <Composer
        sessionId="session-a"
        running
        stopping={false}
        queued={replacement}
        onEvent={onEvent}
        onStop={() => {}}
        draftMemory={draftMemory}
        draftStorage={draftStorage}
      />,
    );

    await user.click(screen.getByRole("button", { name: "Retry clearing follow-up" }));

    expect(onEvent).toHaveBeenCalledTimes(1);
    if (visibleReplacement === null) {
      expect(screen.queryByRole("status", { name: "Queued follow-up" })).not.toBeInTheDocument();
    } else {
      expect(screen.getByRole("status", { name: "Queued follow-up" })).toHaveTextContent(
        visibleReplacement,
      );
    }
    const reconciliation = screen.getByRole("status", { name: "Queue reconciliation" });
    expect(reconciliation).toHaveTextContent("original queue A");
    expect(screen.getByRole("button", { name: "Append cleared follow-up" })).toBeVisible();
    expect(screen.getByRole("button", { name: "Dismiss cleared follow-up" })).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Dismiss cleared follow-up" }));
    expect(screen.queryByRole("status", { name: "Queue reconciliation" })).not.toBeInTheDocument();
    if (visibleReplacement === null) {
      expect(screen.queryByRole("status", { name: "Queued follow-up" })).not.toBeInTheDocument();
    } else {
      expect(screen.getByRole("status", { name: "Queued follow-up" })).toHaveTextContent(
        visibleReplacement,
      );
    }
  });

  it("retries a failed clear exactly once when the controlled queue still matches", async () => {
    const user = userEvent.setup();
    const first = deferred();
    const second = deferred();
    const queued: QueuedMessage = {
      type: "queue_message",
      text: "matching queue A",
      mode: "plan",
    };
    let attempt = 0;
    const onEvent = vi.fn((event: ClientEvent) => {
      if (event.type !== "clear_queued_message") return;
      attempt += 1;
      return attempt === 1 ? first.promise : second.promise;
    });
    const draftMemory = new Map<string, string>();
    const draftStorage = storage().target;
    const { rerender } = renderComposer({
      running: true,
      queued,
      onEvent,
      draftMemory,
      draftStorage,
    });

    await user.click(screen.getByRole("button", { name: "Edit queued follow-up" }));
    await act(async () => {
      first.reject(new Error("offline"));
      await first.promise.catch(() => {});
    });
    rerender(
      <Composer
        sessionId="session-a"
        running
        stopping={false}
        queued={{ ...queued }}
        onEvent={onEvent}
        onStop={() => {}}
        draftMemory={draftMemory}
        draftStorage={draftStorage}
      />,
    );
    await user.click(screen.getByRole("button", { name: "Retry clearing follow-up" }));

    expect(onEvent).toHaveBeenCalledTimes(2);
    expect(onEvent).toHaveBeenLastCalledWith({ type: "clear_queued_message" });
    await act(async () => {
      second.resolve();
      await second.promise;
    });
    expect(screen.getByRole("textbox", { name: "Message" })).toHaveValue("matching queue A");
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
    expect(onEvent).not.toHaveBeenCalled();
    expect(textbox).toHaveValue("計画");

    fireEvent.keyDown(textbox, { key: "Enter", metaKey: true });
    expect(onEvent).toHaveBeenCalledWith({ type: "send_message", text: "計画", mode: "base" });
  });

  it("does not select a slash suggestion with composition or post-composition Enter", () => {
    renderComposer();
    const textbox = screen.getByRole("textbox", { name: "Message" });
    fireEvent.change(textbox, { target: { value: "/pl" } });
    expect(screen.getByRole("listbox", { name: "Composer commands" })).toBeVisible();

    fireEvent.compositionStart(textbox);
    fireEvent.keyDown(textbox, { key: "Enter", isComposing: true });
    expect(textbox).toHaveValue("/pl");
    expect(screen.getByRole("button", { name: "Base mode" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );

    fireEvent.compositionEnd(textbox);
    fireEvent.keyDown(textbox, { key: "Enter", isComposing: false });
    expect(textbox).toHaveValue("/pl");
    expect(screen.getByRole("button", { name: "Base mode" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );

    fireEvent.keyDown(textbox, { key: "Enter", isComposing: false });
    expect(textbox).toHaveValue("");
    expect(screen.getByRole("button", { name: "Plan mode" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
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

    const messageStatus = await screen.findByRole("status", { name: "Message status" });
    expect(messageStatus).toHaveTextContent("Message not sent");
    expect(messageStatus).toHaveAttribute("data-tone", "error");
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

  it("uses theme-aware error feedback tokens with normal-text contrast", () => {
    expect(tokensCss).toContain("--color-danger-text");
    const dangerText = cssTokenValues("--color-danger-text");
    const surfaces = cssTokenValues("--color-surface");
    const ink = cssTokenValues("--color-ink");
    const mutedInk = cssTokenValues("--color-ink-muted");

    expect(dangerText).toEqual([
      "#9f463f",
      "#d87970",
      "#9f463f",
      "#d87970",
      "var(--color-ink)",
    ]);
    expect(surfaces).toHaveLength(4);
    expect(ink).toHaveLength(4);
    expect(mutedInk.at(-1)).toBe("var(--color-ink)");
    expect(contrastRatio(dangerText[0], surfaces[0])).toBeGreaterThanOrEqual(4.5);
    expect(contrastRatio(dangerText[1], surfaces[1])).toBeGreaterThanOrEqual(4.5);
    expect(contrastRatio(dangerText[2], surfaces[2])).toBeGreaterThanOrEqual(4.5);
    expect(contrastRatio(dangerText[3], surfaces[3])).toBeGreaterThanOrEqual(4.5);
    expect(dangerText.at(-1)).toBe("var(--color-ink)");
    expect(contrastRatio(ink[0], surfaces[0])).toBeGreaterThanOrEqual(4.5);
    expect(contrastRatio(ink[1], surfaces[1])).toBeGreaterThanOrEqual(4.5);
    expect(contrastRatio(mutedInk[0], surfaces[0])).toBeGreaterThanOrEqual(4.5);
    expect(contrastRatio(mutedInk[1], surfaces[1])).toBeGreaterThanOrEqual(4.5);
    expect(contrastRatio(mutedInk[2], surfaces[2])).toBeGreaterThanOrEqual(4.5);
    expect(contrastRatio(mutedInk[3], surfaces[3])).toBeGreaterThanOrEqual(4.5);
  });

  it("deduplicates repeated Command+Enter at the action boundary while editing stays available", async () => {
    const first = deferred();
    const onEvent = vi.fn(() => first.promise);
    renderComposer({ onEvent });
    const textbox = screen.getByRole("textbox", { name: "Message" });
    fireEvent.change(textbox, { target: { value: "send once" } });

    fireEvent.keyDown(textbox, { key: "Enter", metaKey: true });
    fireEvent.keyDown(textbox, { key: "Enter", metaKey: true });

    expect(onEvent).toHaveBeenCalledTimes(1);
    expect(textbox).toBeEnabled();
    fireEvent.change(textbox, { target: { value: "send once, then keep editing" } });
    await act(async () => {
      first.resolve();
      await first.promise;
    });
    expect(textbox).toHaveValue("send once, then keep editing");
  });

  it("keeps the newer failed delivery authoritative when an older success resolves later", async () => {
    const user = userEvent.setup();
    const first = deferred();
    const second = deferred();
    const retry = deferred();
    const attempts = [first, second, retry];
    let attempt = 0;
    const onEvent = vi.fn(() => attempts[attempt++].promise);
    renderComposer({ onEvent });
    const textbox = screen.getByRole("textbox", { name: "Message" });

    await user.type(textbox, "first message");
    await user.keyboard("{Meta>}{Enter}{/Meta}");
    fireEvent.change(textbox, { target: { value: "second message" } });
    await user.keyboard("{Meta>}{Enter}{/Meta}");
    await act(async () => {
      second.reject(new Error("second failed"));
      await second.promise.catch(() => {});
    });
    expect(screen.getByRole("status", { name: "Message status" })).toHaveTextContent(
      "Message not sent",
    );

    await act(async () => {
      first.resolve();
      await first.promise;
    });
    expect(textbox).toHaveValue("second message");
    expect(screen.getByRole("button", { name: "Retry message" })).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Retry message" }));
    expect(onEvent).toHaveBeenNthCalledWith(3, {
      type: "send_message",
      text: "second message",
      mode: "base",
    });
    await act(async () => {
      retry.resolve();
      await retry.promise;
    });
  });

  it("does not clear a same-text re-edit when the older delivery succeeds", async () => {
    const first = deferred();
    renderComposer({ onEvent: () => first.promise });
    const textbox = screen.getByRole("textbox", { name: "Message" });
    fireEvent.change(textbox, { target: { value: "same text" } });
    fireEvent.keyDown(textbox, { key: "Enter", metaKey: true });
    fireEvent.change(textbox, { target: { value: "different text" } });
    fireEvent.change(textbox, { target: { value: "same text" } });

    await act(async () => {
      first.resolve();
      await first.promise;
    });

    expect(textbox).toHaveValue("same text");
  });

  it("does not let prior-session success clear an identical current-session draft", async () => {
    const first = deferred();
    const second = deferred();
    let attempt = 0;
    const onEvent = () => {
      attempt += 1;
      return attempt === 1 ? first.promise : second.promise;
    };
    const memory = new Map<string, string>();
    const draftBackup = storage().target;
    const { rerender } = renderComposer({
      sessionId: "session-one",
      onEvent,
      draftMemory: memory,
      draftStorage: draftBackup,
    });
    const textbox = screen.getByRole("textbox", { name: "Message" });
    fireEvent.change(textbox, { target: { value: "identical message" } });
    fireEvent.keyDown(textbox, { key: "Enter", metaKey: true });

    rerender(
      <Composer
        sessionId="session-two"
        running={false}
        stopping={false}
        queued={null}
        onEvent={onEvent}
        onStop={() => {}}
        draftMemory={memory}
        draftStorage={draftBackup}
      />,
    );
    fireEvent.change(textbox, { target: { value: "identical message" } });
    fireEvent.keyDown(textbox, { key: "Enter", metaKey: true });
    await act(async () => {
      first.resolve();
      await first.promise;
    });
    expect(textbox).toHaveValue("identical message");

    await act(async () => {
      second.resolve();
      await second.promise;
    });
    expect(textbox).toHaveValue("");
  });

  it("debounces an inactive session's successful delivery into its text-only backup", async () => {
    vi.useFakeTimers();
    const delivery = deferred();
    const sessionId = "delivery/session-a";
    const storageKey = "agent-harness:draft:delivery%2Fsession-a";
    const backup = storage({
      [storageKey]: JSON.stringify({ text: "already sent" }),
    });
    const memory = new Map<string, string>();
    const { rerender, unmount } = renderComposer({
      sessionId,
      onEvent: () => delivery.promise,
      draftMemory: memory,
      draftStorage: backup.target,
      backupDelayMs: 250,
    });

    fireEvent.click(screen.getByRole("button", { name: "Send message" }));
    rerender(
      <Composer
        sessionId="session-b"
        running={false}
        stopping={false}
        queued={null}
        onEvent={() => delivery.promise}
        onStop={() => {}}
        draftMemory={memory}
        draftStorage={backup.target}
        backupDelayMs={250}
      />,
    );
    await act(async () => {
      delivery.resolve();
      await delivery.promise;
    });

    expect(backup.values.get(storageKey)).toBe(JSON.stringify({ text: "already sent" }));
    act(() => vi.advanceTimersByTime(249));
    expect(backup.values.get(storageKey)).toBe(JSON.stringify({ text: "already sent" }));
    act(() => vi.advanceTimersByTime(1));
    unmount();

    renderComposer({
      sessionId,
      draftStorage: backup.target,
      backupDelayMs: 250,
      draftMemory: new Map(),
    });
    expect(screen.getByRole("textbox", { name: "Message" })).toHaveValue("");
  });

  it("ignores prior-session rejection while the current session is pending", async () => {
    const first = deferred();
    const second = deferred();
    let attempt = 0;
    const onEvent = () => {
      attempt += 1;
      return attempt === 1 ? first.promise : second.promise;
    };
    const memory = new Map<string, string>();
    const draftBackup = storage().target;
    const { rerender } = renderComposer({
      sessionId: "session-one",
      onEvent,
      draftMemory: memory,
      draftStorage: draftBackup,
    });
    const textbox = screen.getByRole("textbox", { name: "Message" });
    fireEvent.change(textbox, { target: { value: "session one message" } });
    fireEvent.keyDown(textbox, { key: "Enter", metaKey: true });

    rerender(
      <Composer
        sessionId="session-two"
        running={false}
        stopping={false}
        queued={null}
        onEvent={onEvent}
        onStop={() => {}}
        draftMemory={memory}
        draftStorage={draftBackup}
      />,
    );
    fireEvent.change(textbox, { target: { value: "session two message" } });
    fireEvent.keyDown(textbox, { key: "Enter", metaKey: true });
    await act(async () => {
      first.reject(new Error("stale failure"));
      await first.promise.catch(() => {});
    });
    expect(textbox).toHaveValue("session two message");
    expect(screen.queryByRole("status", { name: "Message status" })).not.toBeInTheDocument();

    await act(async () => {
      second.resolve();
      await second.promise;
    });
    expect(textbox).toHaveValue("");
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

  it("ignores an older Stop rejection after a later attempt succeeds", async () => {
    const first = deferred();
    let attempt = 0;
    renderComposer({
      running: true,
      onStop: () => {
        attempt += 1;
        return attempt === 1 ? first.promise : undefined;
      },
    });

    fireEvent.click(screen.getByRole("button", { name: "Stop turn" }));
    fireEvent.click(screen.getByRole("button", { name: "Stop turn" }));
    await act(async () => {
      first.reject(new Error("older failure"));
      await first.promise.catch(() => {});
    });

    expect(attempt).toBe(2);
    expect(screen.getByRole("status", { name: "Turn status" })).toBeEmptyDOMElement();
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
