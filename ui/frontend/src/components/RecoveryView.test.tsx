import axe from "axe-core";
import { act, cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { PlatformAdapter } from "../platform/types";
import { RecoveryView } from "./RecoveryView";

function deferred<T = void>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((accept, decline) => { resolve = accept; reject = decline; });
  return { promise, resolve, reject };
}

function adapter(changes: Partial<PlatformAdapter> = {}): PlatformAdapter {
  return {
    kind: "browser",
    getServiceConnection: async () => ({ baseUrl: "http://127.0.0.1:4010", token: "t".repeat(43) }),
    chooseWorkspace: async () => null,
    notify: async () => {},
    ...changes,
  };
}

afterEach(cleanup);

describe("RecoveryView", () => {
  it.each([
    ["credential_prerequisite", "Run codex login, then retry."],
    ["invalid_workspace", "The workspace is unavailable."],
    ["session_resume_failure", "This session could not be reopened."],
    ["session_resume_error", "This session could not be reopened."],
    ["turn_failure", "The turn stopped before it completed."],
    ["sidecar_second_crash", "The local service stopped again."],
    ["unexpected", "Something went wrong. Your local session data was not changed."],
  ])("maps %s to stable safe recovery copy", (category, copy) => {
    render(
      <RecoveryView
        error={{ category, message: "unsafe provider detail token=secret" }}
        sessionId="session-stable"
        platform={adapter()}
        onLocate={async () => {}}
        onArchive={async () => {}}
      />,
    );
    expect(screen.getByRole("alert")).toHaveTextContent(copy);
    expect(screen.getByRole("alert")).not.toHaveTextContent("token=secret");
  });

  it("locates with the canonical picker result or archives the exact stable session once", async () => {
    const user = userEvent.setup();
    const locate = deferred();
    const archive = deferred();
    const chooseWorkspace = vi.fn().mockResolvedValue("/canonical/replacement");
    const onLocate = vi.fn(() => locate.promise);
    const onArchive = vi.fn(() => archive.promise);
    const { container } = render(
      <RecoveryView
        error={{ category: "invalid_workspace", message: "missing" }}
        sessionId="session-stable"
        platform={adapter({ kind: "tauri", chooseWorkspace })}
        onLocate={onLocate}
        onArchive={onArchive}
      />,
    );

    await user.dblClick(screen.getByRole("button", { name: "Locate workspace" }));
    expect(chooseWorkspace).toHaveBeenCalledTimes(1);
    expect(onLocate).toHaveBeenCalledTimes(1);
    expect(onLocate).toHaveBeenCalledWith("session-stable", "/canonical/replacement");
    await act(async () => locate.resolve());
    await user.dblClick(screen.getByRole("button", { name: "Archive session" }));
    expect(onArchive).toHaveBeenCalledTimes(1);
    expect(onArchive).toHaveBeenCalledWith("session-stable");
    expect((await axe.run(container, { rules: { "color-contrast": { enabled: false } } })).violations).toEqual([]);
    await act(async () => archive.resolve());
  });

  it("recovers after picker cancel or rejection without inventing a frontend path", async () => {
    const user = userEvent.setup();
    const chooseWorkspace = vi.fn().mockResolvedValueOnce(null).mockRejectedValueOnce(new Error("Picker failed"));
    const onLocate = vi.fn();
    render(
      <RecoveryView
        error={{ category: "invalid_workspace", message: "missing" }}
        sessionId="session-stable"
        platform={adapter({ kind: "tauri", chooseWorkspace })}
        onLocate={onLocate}
        onArchive={async () => {}}
      />,
    );
    await user.click(screen.getByRole("button", { name: "Locate workspace" }));
    expect(onLocate).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "Locate workspace" }));
    expect(await screen.findByRole("status", { name: "Recovery action failed" })).toHaveTextContent("Picker failed");
    expect(screen.getByRole("button", { name: "Locate workspace" })).toBeEnabled();
  });

  it("shows only fixed native crash capabilities and contains failures with latest-operation authority", async () => {
    const user = userEvent.setup();
    const restart = deferred();
    const restartService = vi.fn(() => restart.promise);
    const openLogs = vi.fn().mockResolvedValue(undefined);
    const quit = vi.fn().mockResolvedValue(undefined);
    render(
      <RecoveryView
        error={{ category: "sidecar_second_crash", message: "crashed" }}
        sessionId="session-stable"
        platform={adapter({ kind: "tauri", restartService, openLogs, quit })}
        onLocate={async () => {}}
        onArchive={async () => {}}
      />,
    );
    await user.dblClick(screen.getByRole("button", { name: "Restart service" }));
    expect(restartService).toHaveBeenCalledTimes(1);
    await act(async () => restart.reject(new Error("Restart failed")));
    expect(await screen.findByRole("status", { name: "Recovery action failed" })).toHaveTextContent("Restart failed");
    await user.click(screen.getByRole("button", { name: "Open logs" }));
    await user.click(screen.getByRole("button", { name: "Quit" }));
    expect(openLogs).toHaveBeenCalledWith();
    expect(quit).toHaveBeenCalledWith();
  });

  it("omits unavailable host actions in the browser", () => {
    render(
      <RecoveryView
        error={{ category: "sidecar_second_crash", message: "crashed" }}
        sessionId="session-stable"
        platform={adapter()}
        onLocate={async () => {}}
        onArchive={async () => {}}
      />,
    );
    expect(screen.queryByRole("button", { name: "Restart service" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Open logs" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Quit" })).not.toBeInTheDocument();
  });
});
