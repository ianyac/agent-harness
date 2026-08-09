import axe from "axe-core";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { PlanReviewCard } from "./PlanReviewCard";

function deferred() {
  let resolve!: () => void;
  let reject!: (reason: Error) => void;
  const promise = new Promise<void>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

const request = {
  turnId: "turn-plan",
  requestId: "plan-one",
  plan: [
    "## Proposed plan",
    "",
    "1. Update [the file](/work/project/README.md)",
    "2. Verify the result",
    "",
    '<img src=x onerror="window.__unsafe = true">',
  ].join("\n"),
};

afterEach(cleanup);

describe("PlanReviewCard", () => {
  it("renders plans through the safe Markdown path", () => {
    render(
      <PlanReviewCard request={request} resolution={null} active onAnswer={() => {}} />,
    );

    expect(screen.getByRole("heading", { name: "Proposed plan" })).toBeVisible();
    expect(screen.getByText("Verify the result")).toBeVisible();
    expect(screen.getByText("/work/project/README.md")).toBeVisible();
    expect(document.querySelector("img")).toBeNull();
    expect(screen.getByText(/<img src=x onerror/)).toBeVisible();
  });

  it("approves the exact request without a feedback key", async () => {
    const user = userEvent.setup();
    const onAnswer = vi.fn();
    render(
      <PlanReviewCard request={request} resolution={null} active onAnswer={onAnswer} />,
    );

    await user.click(screen.getByRole("button", { name: "Approve plan" }));

    expect(onAnswer).toHaveBeenCalledWith({
      type: "answer_plan",
      request_id: "plan-one",
      approved: true,
    });
    expect(onAnswer.mock.calls[0][0]).not.toHaveProperty("feedback");
  });

  it("reveals optional revision feedback, cancels without sending, and omits empty feedback", async () => {
    const user = userEvent.setup();
    const onAnswer = vi.fn();
    render(
      <PlanReviewCard request={request} resolution={null} active onAnswer={onAnswer} />,
    );

    await user.click(screen.getByRole("button", { name: "Revise plan" }));
    expect(screen.getByRole("textbox", { name: "Revision feedback (optional)" })).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Cancel revision" }));
    expect(screen.queryByRole("textbox", { name: "Revision feedback (optional)" })).not.toBeInTheDocument();
    expect(onAnswer).not.toHaveBeenCalled();

    await user.click(screen.getByRole("button", { name: "Revise plan" }));
    await user.click(screen.getByRole("button", { name: "Send revision" }));
    expect(onAnswer).toHaveBeenLastCalledWith({
      type: "answer_plan",
      request_id: "plan-one",
      approved: false,
    });
    expect(onAnswer.mock.calls[0][0]).not.toHaveProperty("feedback");
  });

  it("includes trimmed non-empty feedback only for revision", async () => {
    const user = userEvent.setup();
    const onAnswer = vi.fn();
    render(
      <PlanReviewCard request={request} resolution={null} active onAnswer={onAnswer} />,
    );

    await user.click(screen.getByRole("button", { name: "Revise plan" }));
    await user.type(
      screen.getByRole("textbox", { name: "Revision feedback (optional)" }),
      "  Add a rollback step.  ",
    );
    await user.click(screen.getByRole("button", { name: "Send revision" }));

    expect(onAnswer).toHaveBeenCalledWith({
      type: "answer_plan",
      request_id: "plan-one",
      approved: false,
      feedback: "Add a rollback step.",
    });
  });

  it("suppresses duplicate submission and keeps the request usable after rejection", async () => {
    const pending = deferred();
    const onAnswer = vi.fn(() => pending.promise);
    render(
      <PlanReviewCard request={request} resolution={null} active onAnswer={onAnswer} />,
    );

    const approve = screen.getByRole("button", { name: "Approve plan" });
    fireEvent.click(approve);
    fireEvent.click(approve);
    expect(onAnswer).toHaveBeenCalledTimes(1);
    expect(approve).toBeDisabled();

    await act(async () => {
      pending.reject(new Error("connection closed"));
      await pending.promise.catch(() => {});
    });

    expect(screen.getByRole("alert")).toHaveTextContent("connection closed");
    expect(approve).toBeEnabled();
    expect(screen.getByRole("group", { name: "Plan review" })).toContainElement(
      document.activeElement as HTMLElement,
    );
  });

  it.each([
    [{ approved: true, feedback: "" }, "Plan approved"],
    [{ approved: false, feedback: "Add verification" }, "Revision requested"],
  ] as const)("renders a persistent authoritative decision", (resolution, label) => {
    render(
      <PlanReviewCard
        request={request}
        resolution={resolution}
        active={false}
        onAnswer={() => {}}
      />,
    );

    expect(screen.getByRole("status", { name: "Plan review resolved" })).toHaveTextContent(label);
    if (!resolution.approved) {
      expect(screen.getByRole("status", { name: "Plan review resolved" })).toHaveTextContent(
        "Add verification",
      );
    }
    expect(screen.queryByRole("button", { name: "Approve plan" })).not.toBeInTheDocument();
  });

  it("focuses on arrival and restores connected focus only after a successful answer", async () => {
    const user = userEvent.setup();
    render(<button type="button">Composer</button>);
    const origin = screen.getByRole("button", { name: "Composer" });
    origin.focus();
    render(
      <PlanReviewCard request={request} resolution={null} active onAnswer={() => {}} />,
    );

    const card = screen.getByRole("group", { name: "Plan review" });
    await waitFor(() => expect(card).toHaveFocus());
    expect(screen.getByRole("status", { name: "Plan review request" })).toHaveTextContent(
      "Plan review requested",
    );
    await user.click(screen.getByRole("button", { name: "Approve plan" }));
    await waitFor(() => expect(origin).toHaveFocus());
  });

  it("does not restore focus after a rejected answer", async () => {
    const originRender = render(<button type="button">Composer</button>);
    const origin = screen.getByRole("button", { name: "Composer" });
    origin.focus();
    const pending = deferred();
    render(
      <PlanReviewCard
        request={request}
        resolution={null}
        active
        onAnswer={() => pending.promise}
      />,
    );
    const card = screen.getByRole("group", { name: "Plan review" });
    await waitFor(() => expect(card).toHaveFocus());
    fireEvent.click(screen.getByRole("button", { name: "Approve plan" }));
    originRender.unmount();
    await act(async () => {
      pending.reject(new Error("retry"));
      await pending.promise.catch(() => {});
    });

    expect(card).toContainElement(document.activeElement as HTMLElement);
  });

  it("has no automated accessibility violations", async () => {
    const { container } = render(
      <PlanReviewCard request={request} resolution={null} active onAnswer={() => {}} />,
    );

    expect((await axe.run(container, {
      rules: { "color-contrast": { enabled: false } },
    })).violations).toEqual([]);
  });
});
