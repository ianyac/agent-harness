import axe from "axe-core";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { SafetySnapshot } from "../../protocol/types";
import { PermissionCard } from "./PermissionCard";

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
  turnId: "turn-one",
  requestId: "permission-one",
  action: "web_fetch",
  scope: '{"url":"https://example.com","headers":{"accept":"application/json"}}',
  reason: "Current documentation is required",
};

const safety: SafetySnapshot = {
  mode: "readOnly",
  tools: {
    web_fetch: {
      decision: "ask",
      read_only: true,
      network_egress: true,
    },
  },
};

afterEach(cleanup);

describe("PermissionCard", () => {
  it("shows the action, one requested-action representation, reason, and authoritative network egress", () => {
    render(
      <PermissionCard
        request={request}
        resolution={null}
        active
        safety={safety}
        onAnswer={() => {}}
      />,
    );

    expect(screen.getByRole("heading", { name: "Permission required" })).toBeVisible();
    expect(screen.getByText("web_fetch")).toBeVisible();
    expect(screen.getByLabelText("Requested action")).toHaveTextContent(
      '"url": "https://example.com"',
    );
    expect(screen.queryByText("Scope")).not.toBeInTheDocument();
    expect(screen.queryByText(request.scope)).not.toBeInTheDocument();
    expect(screen.getByText("Current documentation is required")).toBeVisible();
    expect(screen.getByText(/Network access.*sends data outside the workspace/i)).toBeVisible();
  });

  it("shows only the raw scope row when the scope is not JSON", () => {
    render(
      <PermissionCard
        request={{ ...request, action: "write_file", scope: "/work/project/README.md" }}
        resolution={null}
        active
        safety={null}
        onAnswer={() => {}}
      />,
    );

    expect(screen.getByText("Scope")).toBeVisible();
    expect(screen.getByText("/work/project/README.md")).toBeVisible();
    expect(screen.queryByLabelText("Requested action")).not.toBeInTheDocument();
    expect(screen.queryByText(/Network access/i)).not.toBeInTheDocument();
  });

  it.each([
    ["Allow once", "yes"],
    ["Deny", "no"],
    ["Always for this session", "always"],
  ] as const)("sends %s for the exact request", async (name, answer) => {
    const user = userEvent.setup();
    const onAnswer = vi.fn();
    render(
      <PermissionCard
        request={request}
        resolution={null}
        active
        safety={safety}
        onAnswer={onAnswer}
      />,
    );

    await user.click(screen.getByRole("button", { name: new RegExp(`^${name}`) }));

    expect(onAnswer).toHaveBeenCalledWith({
      type: "answer_permission",
      request_id: "permission-one",
      answer,
    });
  });

  it("suppresses duplicate answers synchronously and recovers after rejection", async () => {
    const pending = deferred();
    const onAnswer = vi.fn(() => pending.promise);
    render(
      <PermissionCard
        request={request}
        resolution={null}
        active
        safety={safety}
        onAnswer={onAnswer}
      />,
    );

    const allow = screen.getByRole("button", { name: /^Allow once/ });
    fireEvent.click(allow);
    fireEvent.click(allow);
    expect(onAnswer).toHaveBeenCalledTimes(1);
    expect(allow).toBeDisabled();

    await act(async () => {
      pending.reject(new Error("socket unavailable"));
      await pending.promise.catch(() => {});
    });

    expect(screen.getByRole("alert")).toHaveTextContent("socket unavailable");
    expect(allow).toBeEnabled();
    expect(screen.getByRole("group", { name: "Permission decision" })).toContainElement(
      document.activeElement as HTMLElement,
    );
  });

  it("keeps an immediately sent answer locked until authoritative resolution", async () => {
    const onAnswer = vi.fn();
    render(
      <PermissionCard
        request={request}
        resolution={null}
        active
        safety={safety}
        onAnswer={onAnswer}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: /^Allow once/ }));
    await act(async () => Promise.resolve());
    const card = screen.getByRole("group", { name: "Permission decision" });
    expect(screen.getByRole("status", { name: "Permission submission" })).toHaveTextContent(
      "Decision sent. Waiting for confirmation.",
    );
    expect(screen.getByRole("button", { name: /^Allow once/ })).toBeDisabled();
    expect(screen.getByRole("button", { name: /^Deny/ })).toBeDisabled();

    fireEvent.click(screen.getByRole("button", { name: /^Deny/ }));
    card.focus();
    fireEvent.keyDown(card, { key: "s" });
    expect(onAnswer).toHaveBeenCalledTimes(1);
    expect(onAnswer).toHaveBeenCalledWith(expect.objectContaining({ answer: "yes" }));
  });

  it("lets authoritative resolution dominate a still-pending local dispatch", async () => {
    const pending = deferred();
    const { rerender } = render(
      <PermissionCard
        request={request}
        resolution={null}
        active
        safety={safety}
        onAnswer={() => pending.promise}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: /^Allow once/ }));
    expect(screen.getByRole("status", { name: "Permission submission" })).toHaveTextContent(
      "Sending decision",
    );

    rerender(
      <PermissionCard
        request={request}
        resolution={{ answer: "yes" }}
        active={false}
        safety={safety}
        onAnswer={() => pending.promise}
      />,
    );
    expect(screen.getByRole("status", { name: "Permission resolved" })).toHaveTextContent(
      "Allowed once",
    );
    expect(screen.queryByRole("status", { name: "Permission submission" })).not.toBeInTheDocument();
    expect(screen.queryByText(/Waiting for confirmation/)).not.toBeInTheDocument();

    await act(async () => {
      pending.resolve();
      await pending.promise;
    });
    expect(screen.getByRole("status", { name: "Permission resolved" })).toHaveTextContent(
      "Allowed once",
    );
  });

  it("labels a retained terminal request without claiming an active wait", () => {
    render(
      <PermissionCard
        request={request}
        resolution={null}
        active={false}
        turnRunning={false}
        safety={safety}
        onAnswer={() => {}}
      />,
    );

    expect(screen.getByRole("status", { name: "Permission unresolved" })).toHaveTextContent(
      "Turn ended without a recorded decision.",
    );
    expect(screen.queryByText("Awaiting an authoritative decision")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Allow once/ })).not.toBeInTheDocument();
  });

  it.each([
    ["yes", "Allowed once"],
    ["no", "Denied"],
    ["always", "Always allowed for this session"],
  ] as const)("renders authoritative %s copy without active controls", (answer, copy) => {
    render(
      <PermissionCard
        request={request}
        resolution={{ answer }}
        active={false}
        safety={safety}
        onAnswer={() => {}}
      />,
    );

    expect(screen.getByRole("status", { name: "Permission resolved" })).toHaveTextContent(copy);
    expect(screen.queryByRole("group", { name: "Permission decision" })).not.toBeInTheDocument();
  });

  it("collapses request detail behind a Details disclosure once resolved", () => {
    render(
      <PermissionCard
        request={request}
        resolution={{ answer: "yes" }}
        active={false}
        safety={safety}
        onAnswer={() => {}}
      />,
    );

    expect(screen.getByRole("heading", { name: "Permission required" })).toBeVisible();
    expect(screen.getByText("web_fetch")).toBeVisible();
    expect(screen.getByRole("status", { name: "Permission resolved" })).toBeVisible();
    expect(screen.getByText("Details")).toBeVisible();
    expect(screen.getByText("Current documentation is required")).not.toBeVisible();
    expect(screen.getByLabelText("Requested action")).not.toBeVisible();

    fireEvent.click(screen.getByText("Details"));

    expect(screen.getByText("Current documentation is required")).toBeVisible();
  });

  it("keeps request detail expanded while the request is active", () => {
    render(
      <PermissionCard
        request={request}
        resolution={null}
        active
        safety={safety}
        onAnswer={() => {}}
      />,
    );

    expect(screen.queryByText("Details")).not.toBeInTheDocument();
    expect(screen.getByText("Current documentation is required")).toBeVisible();
  });

  it("focuses a newly active request, announces its reason, and restores connected focus after authority resolves", async () => {
    const user = userEvent.setup();
    render(<button type="button">Previous target</button>);
    const previous = screen.getByRole("button", { name: "Previous target" });
    previous.focus();
    const onAnswer = vi.fn();
    const cardView = render(
      <PermissionCard
        request={request}
        resolution={null}
        active
        safety={safety}
        onAnswer={onAnswer}
      />,
    );

    const card = screen.getByRole("group", { name: "Permission decision" });
    await waitFor(() => expect(card).toHaveFocus());
    expect(screen.getByRole("status", { name: "Permission request" })).toHaveTextContent(
      "Current documentation is required",
    );

    await user.click(screen.getByRole("button", { name: /^Deny/ }));
    await waitFor(() => expect(card).toHaveFocus());
    cardView.rerender(
      <PermissionCard
        request={request}
        resolution={{ answer: "no" }}
        active={false}
        safety={safety}
        onAnswer={onAnswer}
      />,
    );
    await waitFor(() => expect(previous).toHaveFocus());
  });

  it("does not restore a focus origin that was removed before success", async () => {
    const pending = deferred();
    const { unmount: unmountOrigin } = render(<button type="button">Temporary target</button>);
    screen.getByRole("button", { name: "Temporary target" }).focus();
    render(
      <PermissionCard
        request={request}
        resolution={null}
        active
        safety={safety}
        onAnswer={() => pending.promise}
      />,
    );
    await waitFor(() => expect(screen.getByRole("group", { name: "Permission decision" })).toHaveFocus());
    fireEvent.click(screen.getByRole("button", { name: /^Allow once/ }));
    unmountOrigin();
    await act(async () => {
      pending.resolve();
      await pending.promise;
    });

    expect(document.activeElement).not.toHaveTextContent("Temporary target");
  });

  it("shows mnemonic hints and handles them only while focus is inside the card", () => {
    const onAnswer = vi.fn();
    render(
      <>
        <button type="button">Outside</button>
        <PermissionCard
          request={request}
          resolution={null}
          active
          safety={safety}
          onAnswer={onAnswer}
        />
      </>,
    );
    expect(screen.getByRole("button", { name: /^Allow once/ })).toHaveTextContent("A");
    expect(screen.getByRole("button", { name: /^Deny/ })).toHaveTextContent("D");
    expect(screen.getByRole("button", { name: /^Always for this session/ })).toHaveTextContent("S");

    const outside = screen.getByRole("button", { name: "Outside" });
    outside.focus();
    fireEvent.keyDown(outside, { key: "a" });
    expect(onAnswer).not.toHaveBeenCalled();

    const card = screen.getByRole("group", { name: "Permission decision" });
    card.focus();
    fireEvent.keyDown(card, { key: "a" });
    expect(onAnswer).toHaveBeenCalledWith(expect.objectContaining({ answer: "yes" }));
  });

  it("has no automated accessibility violations", async () => {
    const { container } = render(
      <PermissionCard
        request={request}
        resolution={null}
        active
        safety={safety}
        onAnswer={() => {}}
      />,
    );

    expect((await axe.run(container, {
      rules: { "color-contrast": { enabled: false } },
    })).violations).toEqual([]);
  });
});
