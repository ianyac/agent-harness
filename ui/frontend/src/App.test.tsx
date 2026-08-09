import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { App } from "./App";
import { ApiClient } from "./api/http";
import type { SessionRecord } from "./features/sessions/useSessions";

const connection = {
  baseUrl: "http://127.0.0.1:4010",
  token: "t".repeat(43),
};

const storage = {
  getItem: () => null,
  setItem: () => {},
};

function session(changes: Partial<SessionRecord> = {}): SessionRecord {
  return {
    session_id: "session-a",
    workspace: "/work/project-a",
    title: "Project A",
    mode: "default",
    context_mode: "compaction",
    created_at: "2026-08-09T04:00:00.000000+00:00",
    updated_at: "2026-08-09T04:00:00.000000+00:00",
    last_opened_at: "2026-08-09T04:00:00.000000+00:00",
    archived_at: null,
    ...changes,
  };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((accept) => {
    resolve = accept;
  });
  return { promise, resolve };
}

function clientWith(fetchRequest: typeof fetch) {
  return new ApiClient(connection, fetchRequest);
}

afterEach(cleanup);

describe("App", () => {
  it("renders the focused product shell and reports a failed bootstrap", async () => {
    render(
      <App
        sidebarStorage={storage}
      />,
    );

    expect(screen.getByRole("navigation", { name: "Sessions" })).toBeVisible();
    expect(screen.getByRole("main")).toBeVisible();
    expect(screen.getByRole("status", { name: "Local service connecting" })).toBeVisible();
    expect(
      await screen.findByRole("status", { name: "Local service disconnected" }),
    ).toBeVisible();
  });

  it("cancels a session-bound accept-all confirmation when the active session changes", async () => {
    const user = userEvent.setup();
    const created = deferred<Response>();
    const onSessionEvent = vi.fn();
    let createRequests = 0;
    const fetchRequest: typeof fetch = async (input, init) => {
      const url = new URL(input instanceof Request ? input.url : input.toString());
      if (url.pathname === "/api/config") {
        return Response.json({
          base_workspace: "/work/project-b",
          default_mode: "default",
          default_context_mode: "compaction",
          modes: ["default", "acceptAll", "readOnly"],
          context_modes: ["compaction", "folding"],
        });
      }
      if (url.pathname === "/api/sessions" && init?.method === "GET") {
        return Response.json([session()]);
      }
      if (url.pathname === "/api/sessions" && init?.method === "POST") {
        createRequests += 1;
        return created.promise;
      }
      return new Response(null, { status: 404 });
    };

    render(
      <App
        client={clientWith(fetchRequest)}
        sidebarStorage={storage}
        onSessionEvent={onSessionEvent}
      />,
    );
    await screen.findByRole("heading", { name: "project-a" });
    expect(screen.getByRole("status", { name: "Local service connected" })).toBeVisible();

    await user.keyboard("{Meta>}n{/Meta}");
    expect(createRequests).toBe(1);
    await user.click(screen.getByRole("button", { name: "Permission mode: Default" }));
    await user.click(screen.getByRole("menuitemradio", { name: "Accept all" }));
    expect(screen.getByRole("dialog", { name: "Enable accept all?" })).toBeVisible();

    await user.keyboard("{Meta>}k{/Meta}");
    await user.keyboard("{Meta>}n{/Meta}");
    expect(createRequests).toBe(1);
    expect(screen.queryByRole("dialog", { name: "Command palette" })).not.toBeInTheDocument();

    created.resolve(
      Response.json(
        session({ session_id: "session-b", workspace: "/work/project-b", title: "Project B" }),
        { status: 201 },
      ),
    );

    await screen.findByRole("heading", { name: "project-b" });
    await waitFor(() =>
      expect(screen.queryByRole("dialog", { name: "Enable accept all?" })).not.toBeInTheDocument(),
    );
    expect(onSessionEvent).not.toHaveBeenCalled();
  });
});
