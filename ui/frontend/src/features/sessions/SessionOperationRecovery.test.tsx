import { act, cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { SessionOperationRecovery } from "./SessionOperationRecovery";

afterEach(cleanup);

describe("SessionOperationRecovery", () => {
  it("offers stable duplicate-suppressed manual cleanup recovery", async () => {
    const user = userEvent.setup();
    let resolveCleanup!: () => void;
    const pending = new Promise<void>((resolve) => {
      resolveCleanup = resolve;
    });
    const retry = vi.fn(() => pending);
    render(
      <SessionOperationRecovery
        failure={{ kind: "cleanup", sessionId: "private-stale-id" }}
        onRetry={retry}
      />,
    );

    const alert = screen.getByRole("alert", { name: "Session operation failed" });
    expect(alert).toHaveTextContent("Cleanup needs another try");
    expect(alert).toHaveTextContent("An unused session is hidden until cleanup is confirmed.");
    expect(alert).not.toHaveTextContent("private-stale-id");
    await user.dblClick(screen.getByRole("button", { name: "Retry Cleanup" }));
    expect(retry).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("button", { name: "Retry Cleanup" })).toBeDisabled();

    await act(async () => resolveCleanup());
    expect(screen.getByRole("button", { name: "Retry Cleanup" })).toBeEnabled();
  });

  it("renders as a compact dismissible banner rather than a page-level takeover", async () => {
    const user = userEvent.setup();
    const onDismiss = vi.fn();
    render(
      <SessionOperationRecovery
        failure={{
          kind: "rename",
          sessionId: "session-1",
          title: "Next title",
          error: new Error("boom"),
        }}
        onRetry={() => {}}
        onDismiss={onDismiss}
      />,
    );

    const alert = screen.getByRole("alert", { name: "Session operation failed" });
    expect(alert).toHaveTextContent("Session wasn’t renamed");
    expect(alert).toHaveTextContent("The previous session name is still in use.");
    expect(screen.queryByRole("heading")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Retry Rename" })).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Dismiss" }));
    expect(onDismiss).toHaveBeenCalledTimes(1);
  });

  it("omits the dismiss control until a dismiss handler is wired", () => {
    render(
      <SessionOperationRecovery
        failure={{ kind: "cleanup", sessionId: "session-1" }}
        onRetry={() => {}}
      />,
    );
    expect(screen.queryByRole("button", { name: "Dismiss" })).not.toBeInTheDocument();
  });
});
