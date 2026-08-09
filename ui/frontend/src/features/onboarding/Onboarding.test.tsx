import axe from "axe-core";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "../../api/http";
import type { PlatformAdapter } from "../../platform/types";
import type { CreateSessionOptions, CreateSessionResult, SessionRecord } from "../sessions/useSessions";
import { Onboarding } from "./Onboarding";

function session(workspace = "/work/acme"): SessionRecord {
  return {
    session_id: "first-session",
    workspace,
    title: "New chat",
    mode: "default",
    context_mode: "compaction",
    created_at: "2026-08-09T04:00:00Z",
    updated_at: "2026-08-09T04:00:00Z",
    last_opened_at: "2026-08-09T04:00:00Z",
    archived_at: null,
  };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((accept, decline) => {
    resolve = accept;
    reject = decline;
  });
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

const success = (workspace = "/work/acme"): CreateSessionResult => ({
  ok: true,
  session: session(workspace),
});

afterEach(cleanup);

describe("Onboarding", () => {
  it("shows an explicit quiet readiness state before workspace selection", () => {
    render(
      <Onboarding
        serviceStatus="checking"
        platform={adapter()}
        defaultWorkspace="/work/acme"
        defaultMode="default"
        defaultContextMode="compaction"
        onCreate={async () => success()}
      />,
    );
    expect(screen.getByRole("status", { name: "Checking local service" })).toBeVisible();
    expect(screen.queryByRole("textbox", { name: "Workspace path" })).not.toBeInTheDocument();
  });

  it("guards browser syntax, explains local trust, and forwards typed invalid_workspace copy", async () => {
    const user = userEvent.setup();
    const onCreate = vi.fn(async (_options: CreateSessionOptions): Promise<CreateSessionResult> => ({
      ok: false,
      error: new ApiError(422, "invalid_workspace", "That workspace is not available."),
    }));
    const { container } = render(
      <Onboarding
        serviceStatus="ready"
        platform={adapter()}
        defaultWorkspace=""
        defaultMode="default"
        defaultContextMode="compaction"
        onCreate={onCreate}
      />,
    );

    expect(screen.getByText(/processing and session files stay local/i)).toBeVisible();
    expect(screen.getByText(/does not automatically run workspace configuration/i)).toBeVisible();
    const path = screen.getByRole("textbox", { name: "Workspace path" });
    for (const invalid of ["", "relative/path", " /work/acme", "/work/acme "]) {
      await user.clear(path);
      if (invalid !== "") await user.type(path, invalid);
      await user.click(screen.getByRole("button", { name: "Start local session" }));
      expect(onCreate).not.toHaveBeenCalled();
    }
    fireEvent.change(path, { target: { value: "/work/\0bad" } });
    await user.click(screen.getByRole("button", { name: "Start local session" }));
    expect(onCreate).not.toHaveBeenCalled();

    await user.clear(path);
    await user.type(path, "/missing/acme");
    await user.click(screen.getByRole("button", { name: "Start local session" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("That workspace is not available.");
    expect(screen.getByRole("alert")).not.toHaveTextContent(/verified|client validated/i);
    expect((await axe.run(container, { rules: { "color-contrast": { enabled: false } } })).violations).toEqual([]);
  });

  it("keeps the mounted native picker single-flight with a visible disabled pending state", async () => {
    const user = userEvent.setup();
    const picker = deferred<string | null>();
    const chooseWorkspace = vi.fn(() => picker.promise);
    render(
      <Onboarding
        serviceStatus="ready"
        platform={adapter({ kind: "tauri", chooseWorkspace })}
        defaultWorkspace=""
        defaultMode="default"
        defaultContextMode="compaction"
        onCreate={async () => success()}
      />,
    );

    await user.dblClick(screen.getByRole("button", { name: "Choose folder" }));
    expect(chooseWorkspace).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("button", { name: "Choosing folder…" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Start local session" })).toBeDisabled();

    await act(async () => picker.resolve("/canonical/native-workspace"));
    expect(screen.getByText("/canonical/native-workspace")).toBeVisible();
    expect(screen.getByRole("button", { name: "Choose folder" })).toBeEnabled();
  });

  it("contains native picker rejection behind generic retry and clears cleanly on cancel", async () => {
    const user = userEvent.setup();
    const chooseWorkspace = vi.fn()
      .mockRejectedValueOnce(new Error("/private/workspace/native-dialog-detail"))
      .mockResolvedValueOnce(null);
    const onCreate = vi.fn(async () => success());
    render(
      <Onboarding
        serviceStatus="ready"
        platform={adapter({ kind: "tauri", chooseWorkspace })}
        defaultWorkspace=""
        defaultMode="default"
        defaultContextMode="compaction"
        onCreate={onCreate}
      />,
    );

    await user.click(screen.getByRole("button", { name: "Choose folder" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "The folder picker is unavailable. Try again.",
    );
    expect(screen.getByRole("alert")).not.toHaveTextContent(/private|workspace|native-dialog-detail/i);

    await user.click(screen.getByRole("button", { name: "Retry folder picker" }));
    expect(chooseWorkspace).toHaveBeenCalledTimes(2);
    await waitFor(() => expect(screen.queryByRole("alert")).not.toBeInTheDocument());
    expect(screen.queryByText(/canonical\//)).not.toBeInTheDocument();
    expect(onCreate).not.toHaveBeenCalled();
  });

  it("uses only the successful native picker result for session creation", async () => {
    const user = userEvent.setup();
    const chooseWorkspace = vi.fn().mockResolvedValue("/canonical/native-workspace");
    const onCreate = vi.fn(async () => success("/canonical/native-workspace"));
    render(
      <Onboarding
        serviceStatus="ready"
        platform={adapter({ kind: "tauri", chooseWorkspace })}
        defaultWorkspace=""
        defaultMode="default"
        defaultContextMode="compaction"
        onCreate={onCreate}
      />,
    );

    expect(screen.queryByRole("textbox", { name: "Workspace path" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Choose folder" }));
    expect(screen.getByText("/canonical/native-workspace")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Start local session" }));
    expect(onCreate).toHaveBeenCalledWith({
      workspace: "/canonical/native-workspace",
      mode: "default",
      contextMode: "compaction",
      title: "New chat",
    }, expect.anything());
  });

  it("never presents the application bootstrap directory as a native user workspace", async () => {
    const user = userEvent.setup();
    const onCreate = vi.fn(async () => success());
    render(
      <Onboarding
        serviceStatus="ready"
        platform={adapter({ kind: "tauri" })}
        defaultWorkspace="/Users/test/Library/Application Support/Harness/service-bootstrap"
        defaultMode="default"
        defaultContextMode="compaction"
        onCreate={onCreate}
      />,
    );

    expect(screen.queryByText(/service-bootstrap/)).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Start local session" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Choose a workspace");
    expect(onCreate).not.toHaveBeenCalled();
  });

  it("sends one base and one context mode, retains credential request, and suppresses rapid retry duplicates", async () => {
    const user = userEvent.setup();
    const retry = deferred<CreateSessionResult>();
    const onCreate = vi.fn()
      .mockResolvedValueOnce({
        ok: false,
        error: new ApiError(409, "credential_prerequisite", "Sign in is required."),
      })
      .mockReturnValueOnce(retry.promise);
    render(
      <Onboarding
        serviceStatus="ready"
        platform={adapter()}
        defaultWorkspace="/work/acme"
        defaultMode="default"
        defaultContextMode="compaction"
        onCreate={onCreate}
      />,
    );

    await user.selectOptions(screen.getByRole("combobox", { name: "Permission mode" }), "readOnly");
    await user.click(screen.getByRole("radio", { name: "Recoverable folding" }));
    await user.click(screen.getByRole("button", { name: "Start local session" }));
    expect(await screen.findByRole("heading", { name: "Sign in required" })).toBeVisible();
    expect(screen.getByText("codex login")).toBeVisible();
    expect(document.body).not.toHaveTextContent(/bearer|access token|refresh token/i);
    expect(onCreate).toHaveBeenNthCalledWith(1, {
      workspace: "/work/acme",
      mode: "readOnly",
      contextMode: "folding",
      title: "New chat",
    }, expect.anything());

    const retryButton = screen.getByRole("button", { name: "Retry" });
    await user.dblClick(retryButton);
    expect(onCreate).toHaveBeenCalledTimes(2);
    expect(onCreate).toHaveBeenNthCalledWith(2, {
      workspace: "/work/acme",
      mode: "readOnly",
      contextMode: "folding",
      title: "New chat",
    }, expect.anything());
    await act(async () => retry.resolve(success()));
    await waitFor(() => expect(screen.queryByRole("heading", { name: "Sign in required" })).not.toBeInTheDocument());
  });

  it("ignores a stale submit after workspace and authority replacement", async () => {
    const user = userEvent.setup();
    const stale = deferred<CreateSessionResult>();
    const onCreate = vi.fn().mockReturnValueOnce(stale.promise).mockResolvedValueOnce(success("/work/new"));
    const props = {
      serviceStatus: "ready" as const,
      platform: adapter(),
      defaultMode: "default" as const,
      defaultContextMode: "compaction" as const,
      onCreate,
    };
    const { rerender } = render(<Onboarding {...props} authorityKey="client-a" defaultWorkspace="/work/old" />);
    await user.click(screen.getByRole("button", { name: "Start local session" }));
    rerender(<Onboarding {...props} authorityKey="client-b" defaultWorkspace="/work/new" />);
    await act(async () => stale.resolve({
      ok: false,
      error: new ApiError(409, "credential_prerequisite", "Stale credential error"),
    }));
    expect(screen.queryByText("Stale credential error")).not.toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "Workspace path" })).toHaveValue("/work/new");
  });
});
