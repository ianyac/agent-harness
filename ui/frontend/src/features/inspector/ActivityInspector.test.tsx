import axe from "axe-core";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { emptyTranscript } from "../../protocol/reducer";
import type { TranscriptState } from "../../protocol/types";
import { ActivityInspector } from "./ActivityInspector";

function state(overrides: Partial<TranscriptState> = {}): TranscriptState {
  return {
    ...emptyTranscript(),
    generation: 1,
    lastSequence: 12,
    activities: {
      root: {
        activityId: "root",
        turnId: "turn-1",
        parentActivityId: null,
        actor: "subagent",
        name: "delegate",
        args: { zeta: 2, alpha: "full\nargument" },
        startedAt: "2026-08-08T00:00:00Z",
        status: "complete",
        result: { zeta: "complete result", alpha: [1, 2] },
        isError: false,
        durationMs: 0,
      },
      child: {
        activityId: "child",
        turnId: "turn-1",
        parentActivityId: "root",
        actor: "tool",
        name: "write_file",
        args: { path: "README.md" },
        startedAt: "2026-08-08T00:00:01Z",
        status: "error",
        result: "full result that must never be truncated",
        isError: true,
        durationMs: 8,
      },
    },
    activityOrder: ["root", "child"],
    timeline: [
      { kind: "assistant", text: "Model checked the plan", messageIndex: null },
      { kind: "activity", activityId: "root" },
      { kind: "activity", activityId: "child" },
      {
        kind: "permission",
        request: { turnId: "turn-1", requestId: "p1", action: "write_file", scope: "README.md", reason: "Needs an exact edit" },
        resolution: { answer: "no" },
      },
      {
        kind: "plan_review",
        request: { turnId: "turn-1", requestId: "r1", plan: "1. Verify" },
        resolution: { approved: false, feedback: "Add a test" },
      },
      { kind: "context", turnId: "turn-1", sequence: 10, context: { mode: "compaction", used_tokens: 0, summarized_messages: 3 } },
      { kind: "boundary", boundary: "error" },
      { kind: "boundary", boundary: "turn_completion" },
    ],
    safety: {
      mode: "default",
      sandbox_backend: "SeatbeltSandbox",
      workspace_write_boundary: "/work/acme",
      read_breadth: "host",
      network_policy: "deny",
      tools: {
        write_file: { decision: "ask", read_only: false, network_egress: false },
        web_fetch: { decision: "allow", read_only: true, network_egress: true },
      },
    },
    latestContext: { mode: "compaction", used_tokens: 0, summarized_messages: 3 },
    ...overrides,
  };
}

const noop = () => {};

function renderInspector(overrides: Partial<React.ComponentProps<typeof ActivityInspector>> = {}) {
  return render(
    <ActivityInspector
      open
      narrow={false}
      width={420}
      pinned={false}
      contextMode="folding"
      state={state()}
      selectedActivityId={null}
      onClose={noop}
      onPinnedChange={noop}
      onSelectActivity={noop}
      onWidthChange={noop}
      {...overrides}
    />,
  );
}

afterEach(cleanup);

describe("ActivityInspector", () => {
  it("renders an overview with exact authoritative safety and context truth", () => {
    renderInspector();

    const dialog = screen.getByRole("dialog", { name: "Activity inspector" });
    expect(within(dialog).getByText("SeatbeltSandbox")).toBeVisible();
    expect(within(dialog).getByText("/work/acme")).toBeVisible();
    expect(within(dialog).getByText("host")).toBeVisible();
    expect(within(dialog).getByText("Deny")).toBeVisible();
    expect(within(dialog).getByText("Folding")).toBeVisible();
    expect(within(dialog).getByText("Compaction")).toBeVisible();
    expect(within(dialog).getByText("0", { selector: "dd" })).toBeVisible();
    expect(within(dialog).getByText("3", { selector: "dd" })).toBeVisible();
    expect(within(dialog).getByRole("row", { name: /web fetch Allow Yes Yes/i })).toBeVisible();
    expect(within(dialog).getByRole("row", { name: /write file Ask No No/i })).toBeVisible();
  });

  it("labels absent and malformed safety/context values Unknown instead of guessing", () => {
    renderInspector({
      state: state({
        safety: { sandbox_backend: 4, network_policy: "sometimes", tools: { broken: "yes" } },
        latestContext: { mode: false, used_tokens: "many" },
      }),
    });

    const dialog = screen.getByRole("dialog", { name: "Activity inspector" });
    expect(within(dialog).getAllByText("Unknown").length).toBeGreaterThanOrEqual(6);
    expect(within(dialog).queryByText("Protected")).not.toBeInTheDocument();
    expect(within(dialog).queryByText("No network access")).not.toBeInTheDocument();
  });

  it("keeps chronological row order and computes parent depth without loops", () => {
    const cyclic = state();
    cyclic.activities.root = { ...cyclic.activities.root, parentActivityId: "child" };
    const { rerender } = renderInspector({ state: cyclic });
    const rows = screen.getAllByRole("listitem").map((row) => row.textContent);
    expect(rows).toEqual([
      expect.stringContaining("Model checked the plan"),
      expect.stringContaining("delegate"),
      expect.stringContaining("write file"),
      expect.stringContaining("Needs an exact edit"),
      expect.stringContaining("Verify"),
      expect.stringContaining("Compaction"),
      expect.stringContaining("Error"),
      expect.stringContaining("Turn complete"),
    ]);
    expect(rows[4]).toContain("Add a test");
    expect(screen.getByRole("listitem", { name: /delegate/ })).toHaveAttribute("aria-level", "1");
    expect(screen.getByRole("listitem", { name: /write file/ })).toHaveAttribute("aria-level", "1");

    rerender(
      <ActivityInspector
        open narrow={false} width={420} pinned={false}
        contextMode="folding" state={state()} selectedActivityId={null}
        onClose={noop} onPinnedChange={noop} onSelectActivity={noop} onWidthChange={noop}
      />,
    );
    expect(screen.getByRole("listitem", { name: /delegate/ })).toHaveAttribute("aria-level", "1");
    expect(screen.getByRole("listitem", { name: /write file/ })).toHaveAttribute("aria-level", "2");
  });

  it("shows selected exact activity detail including 0ms, full deterministic payloads, result, and error", async () => {
    const user = userEvent.setup();
    const copyText = vi.fn().mockResolvedValue(undefined);
    renderInspector({ selectedActivityId: "root", copyText });
    const detail = screen.getByRole("region", { name: "Selected activity detail" });
    expect(detail).toHaveTextContent("delegate");
    expect(detail).toHaveTextContent("subagent");
    expect(detail).toHaveTextContent("Complete");
    expect(detail).toHaveTextContent("0ms");
    expect(within(detail).getByText(/"alpha": "full\\nargument"[\s\S]*"zeta": 2/)).toBeVisible();
    expect(within(detail).getByText(/"alpha": \[[\s\S]*"zeta": "complete result"/)).toBeVisible();
    expect(within(detail).getAllByText("No").length).toBeGreaterThan(0);

    const argumentsDetails = within(detail).getByText("Arguments").closest("details");
    expect(argumentsDetails).not.toBeNull();
    await user.click(within(argumentsDetails as HTMLElement).getByRole("button", { name: "Copy arguments" }));
    expect(copyText).toHaveBeenCalledWith('{\n  "alpha": "full\\nargument",\n  "zeta": 2\n}');
    expect(screen.getByRole("status", { name: "Inspector copy status" })).toHaveTextContent("Copied");
  });

  it("announces copy failure and sends complete string results verbatim", async () => {
    const user = userEvent.setup();
    const copyText = vi.fn().mockRejectedValue(new Error("denied"));
    renderInspector({ selectedActivityId: "child", copyText });
    await user.click(screen.getByRole("button", { name: "Copy result" }));
    expect(copyText).toHaveBeenCalledWith("full result that must never be truncated");
    expect(screen.getByRole("status", { name: "Inspector copy status" })).toHaveTextContent(
      "Copy failed. Try again.",
    );
    expect(screen.getByRole("region", { name: "Selected activity detail" })).toHaveTextContent("Error");
  });

  it("exposes clamped pointer and keyboard resizing through an accessible separator", () => {
    const onWidthChange = vi.fn();
    renderInspector({ width: 400, onWidthChange });
    const separator = screen.getByRole("separator", { name: "Resize activity inspector" });
    expect(separator).toHaveAttribute("aria-valuemin", "320");
    expect(separator).toHaveAttribute("aria-valuemax", "640");
    expect(separator).toHaveAttribute("aria-valuenow", "400");
    fireEvent.keyDown(separator, { key: "ArrowLeft" });
    fireEvent.keyDown(separator, { key: "End" });
    expect(onWidthChange).toHaveBeenNthCalledWith(1, 416);
    expect(onWidthChange).toHaveBeenNthCalledWith(2, 640);

    fireEvent(separator, new MouseEvent("pointerdown", {
      bubbles: true,
      button: 0,
      clientX: 400,
    }));
    fireEvent(window, new MouseEvent("pointermove", { bubbles: true, clientX: 900 }));
    fireEvent(window, new MouseEvent("pointerup", { bubbles: true }));
    expect(onWidthChange).toHaveBeenLastCalledWith(320);
  });

  it("removes an active pointer resize operation when the inspector unmounts", () => {
    const onWidthChange = vi.fn();
    const { unmount } = renderInspector({ width: 400, onWidthChange });
    const separator = screen.getByRole("separator", { name: "Resize activity inspector" });
    fireEvent(separator, new MouseEvent("pointerdown", {
      bubbles: true,
      button: 0,
      clientX: 400,
    }));
    unmount();
    fireEvent(window, new MouseEvent("pointermove", { bubbles: true, clientX: 200 }));
    expect(onWidthChange).not.toHaveBeenCalled();
  });

  it("uses truthful wide non-modal and narrow modal semantics with complete labels", () => {
    const { rerender } = renderInspector();
    expect(screen.queryByTestId("inspector-overlay")).not.toBeInTheDocument();
    expect(screen.getByRole("dialog", { name: "Activity inspector" })).toHaveAttribute("data-modal", "false");
    expect(screen.getByText("Review this session’s chronological harness activity and authoritative safety context.")).toBeVisible();

    rerender(
      <ActivityInspector
        open narrow width={420} pinned={false} contextMode="folding"
        state={state()} selectedActivityId={null} onClose={noop} onPinnedChange={noop}
        onSelectActivity={noop} onWidthChange={noop}
      />,
    );
    expect(screen.getByTestId("inspector-overlay")).toBeVisible();
    expect(screen.getByRole("dialog", { name: "Activity inspector" })).toHaveAttribute("data-modal", "true");
  });

  it("has no automated accessibility violations in wide selected/error and narrow overview fixtures", async () => {
    const canvas = document.createElement("canvas");
    const getContext = vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue({
      canvas,
      clearRect: () => {},
      fillText: () => {},
      font: "",
      getImageData: () => ({ data: new Uint8ClampedArray([255, 255, 255, 255]) }),
      measureText: (text: string) => ({ width: Math.max(1, text.length * 8) }),
      textAlign: "left",
      textBaseline: "top",
    } as unknown as CanvasRenderingContext2D);
    const { rerender } = renderInspector();
    expect((await axe.run(document.body)).violations).toEqual([]);
    rerender(
      <ActivityInspector
        open narrow={false} width={420} pinned={false} contextMode="folding"
        state={state()} selectedActivityId="child" onClose={noop} onPinnedChange={noop}
        onSelectActivity={noop} onWidthChange={noop}
      />,
    );
    expect((await axe.run(document.body)).violations).toEqual([]);
    rerender(
      <ActivityInspector
        open narrow width={420} pinned={false} contextMode="folding"
        state={state()} selectedActivityId={null} onClose={noop} onPinnedChange={noop}
        onSelectActivity={noop} onWidthChange={noop}
      />,
    );
    expect((await axe.run(document.body)).violations).toEqual([]);
    getContext.mockRestore();
  });
});
