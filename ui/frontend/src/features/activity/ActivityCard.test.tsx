import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import type { ActivityItem } from "../../protocol/types";
import { ActivityCard } from "./ActivityCard";

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
    result: "done",
    isError: false,
    durationMs: 1_000,
    ...overrides,
  };
}

function card() {
  return screen.getByRole("button", { name: /Open activity/ });
}

describe("ActivityCard", () => {
  it("rolls durations of a minute or more into minutes and seconds", () => {
    render(<ActivityCard activities={[activity({ durationMs: 64_000 })]} openInspector={() => {}} />);

    expect(card()).toHaveTextContent("1m 4s");
  });

  it("reports wall-clock duration for a group instead of summing overlaps", () => {
    const first = activity({ durationMs: 34_000 });
    const second = activity({
      activityId: "activity-2",
      startedAt: "2026-08-08T00:00:30Z",
      durationMs: 10_000,
    });
    render(<ActivityCard activities={[first, second]} openInspector={() => {}} />);

    expect(card()).toHaveTextContent("40s");
    expect(card()).not.toHaveTextContent("44s");
  });

  it("falls back to summing when a timed member has no recorded start", () => {
    const first = activity({ durationMs: 2_000 });
    const second = activity({ activityId: "activity-2", startedAt: "", durationMs: 3_000 });
    render(<ActivityCard activities={[first, second]} openInspector={() => {}} />);

    expect(card()).toHaveTextContent("5s");
  });

  it("omits the duration segment for historical activities without timing data", () => {
    const historical = activity({ startedAt: "", durationMs: undefined });
    render(<ActivityCard activities={[historical]} openInspector={() => {}} />);

    expect(card()).not.toHaveTextContent("NaN");
    expect(card()).not.toHaveTextContent("0ms");
    expect(card()).toHaveTextContent("Complete");
    expect(card()).toHaveTextContent("1 action");
  });

  it("tolerates a running activity with no recorded start", () => {
    const running = activity({
      status: "running",
      startedAt: "",
      result: undefined,
      isError: undefined,
      durationMs: undefined,
    });
    render(<ActivityCard activities={[running]} openInspector={() => {}} />);

    expect(card()).not.toHaveTextContent("NaN");
    expect(card()).not.toHaveTextContent("0ms");
    expect(card()).toHaveTextContent("Working");
  });

  it("still shows a legitimate zero duration", () => {
    render(<ActivityCard activities={[activity({ durationMs: 0 })]} openInspector={() => {}} />);

    expect(card()).toHaveTextContent("0ms");
  });

  it("truncates the serialized result preview before rendering", () => {
    const long = activity({ result: "y".repeat(2_000) });
    render(<ActivityCard activities={[long]} openInspector={() => {}} />);

    const preview = within(card()).getByText(/y{100}/);
    expect(preview.textContent).toHaveLength(500);
  });
});
