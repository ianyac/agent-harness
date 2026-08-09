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
    await user.dblClick(screen.getByRole("button", { name: "Retry cleanup" }));
    expect(retry).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("button", { name: "Retry cleanup" })).toBeDisabled();

    await act(async () => resolveCleanup());
    expect(screen.getByRole("button", { name: "Retry cleanup" })).toBeEnabled();
  });
});
