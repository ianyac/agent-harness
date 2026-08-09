import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useRef } from "react";
import { afterEach, describe, expect, it } from "vitest";

import { ApiClient } from "../../api/http";
import type { SessionRecord } from "./useSessions";
import { createSessionAuthority, useSessions } from "./useSessions";

const connection = {
  baseUrl: "http://127.0.0.1:4010",
  token: "t".repeat(43),
};
const testServiceId = "11111111-1111-4111-8111-111111111111";

const config = {
  base_workspace: "/work/acme",
  default_mode: "default",
  default_context_mode: "compaction",
  modes: ["default", "acceptAll", "readOnly"],
  context_modes: ["compaction", "folding"],
};

function session(id: string, title = id): SessionRecord {
  return {
    session_id: id,
    workspace: "/work/acme",
    title,
    mode: "default",
    context_mode: "compaction",
    created_at: "2026-08-09T04:00:00.000000+00:00",
    updated_at: "2026-08-09T04:00:00.000000+00:00",
    last_opened_at: "2026-08-09T04:00:00.000000+00:00",
    archived_at: null,
  };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((accept) => {
    resolve = accept;
  });
  return { promise, resolve };
}

function client(fetchRequest: typeof fetch, useFetchHealth = false) {
  const withIdentity: typeof fetch = async (input, init) => {
    const path = new URL(input instanceof Request ? input.url : input.toString()).pathname;
    if (path === "/api/health" && !useFetchHealth) {
      return Response.json({ status: "ok", service_id: testServiceId });
    }
    return fetchRequest(input, init);
  };
  return new ApiClient(connection, withIdentity);
}

function SessionsHarness({ api, storage }: { api: ApiClient; storage?: LedgerStorage }) {
  const model = useSessions(api, storage);
  return (
    <>
      <output aria-label="Session titles">{model.sessions.map((item) => item.title).join("|")}</output>
      <output aria-label="Session readiness">{model.loading ? "loading" : "ready"}</output>
      <output aria-label="Config readiness">{model.config === null ? "none" : "ready"}</output>
      <output aria-label="Active session">{model.activeSessionId ?? "none"}</output>
      <output aria-label="Refresh error">{model.refreshError?.message ?? "none"}</output>
      <output aria-label="Operation error">
        {model.operationError === null
          ? "none"
          : `${model.operationError.kind}:${"category" in model.operationError.error
            ? String(model.operationError.error.category)
            : "unknown"}:${model.operationError.error.message}`}
      </output>
      <button type="button" onClick={() => void model.refresh()}>Refresh</button>
      <button type="button" onClick={() => void model.createSession()}>Create</button>
      <button type="button" onClick={() => void model.createSession({
        workspace: "/work/explicit",
        mode: "readOnly",
        contextMode: "folding",
        title: "Explicit chat",
      })}>Create explicit</button>
      <button type="button" onClick={() => void model.createSession({
        workspace: "relative/work",
        mode: "readOnly",
        contextMode: "folding",
      })}>Create invalid</button>
      <button type="button" onClick={() => model.selectSession("session-1")}>Select session-1</button>
      <button type="button" onClick={() => model.selectSession("session-2")}>Select session-2</button>
      <button type="button" onClick={() => void model.renameSession("session-1", "First rename")}>
        First rename
      </button>
      <button type="button" onClick={() => void model.renameSession("session-1", "Second rename")}>
        Second rename
      </button>
      <button type="button" onClick={() => void model.archiveSession("session-1")}>Archive</button>
      <button type="button" onClick={() => void model.retryOperation()}>Retry operation</button>
    </>
  );
}

type LedgerStorage = {
  readonly getItem: (key: string) => string | null;
  readonly setItem: (key: string, value: string) => void;
};

function CleanupHarness({ api, storage }: { api: ApiClient; storage: LedgerStorage }) {
  const authority = useRef(createSessionAuthority());
  const model = useSessions(api, storage);
  return (
    <>
      <output aria-label="Session titles">{model.sessions.map((item) => item.title).join("|")}</output>
      <output aria-label="Session readiness">{model.loading ? "loading" : "ready"}</output>
      <output aria-label="Refresh error">{model.refreshError?.message ?? "none"}</output>
      <output aria-label="Cleanup status">
        {model.cleanupError === null ? "none" : `cleanup:${model.cleanupError.sessionId}`}
      </output>
      <button type="button" onClick={() => void model.createSession({
        workspace: "/work/stale",
        mode: "default",
        contextMode: "compaction",
        title: "Stale create",
      }, authority.current)}>Create stale</button>
      <button type="button" onClick={() => model.invalidateCreate(authority.current)}>Supersede</button>
      <button type="button" onClick={() => void model.renameSession("current", "Current renamed")}>Rename current</button>
      <button type="button" onClick={() => void model.refresh()}>Refresh</button>
      <button type="button" onClick={() => void model.retryCleanup()}>Retry cleanup</button>
    </>
  );
}

afterEach(cleanup);

describe("useSessions async authority", () => {
  it("requires a valid service identity before committing config, sessions, or create readiness", async () => {
    const user = userEvent.setup();
    const serviceId = "11111111-1111-4111-8111-111111111111";
    let health: "failed" | "invalid" | "valid" = "failed";
    let creates = 0;
    const storage: LedgerStorage = {
      getItem: (key) => key.includes(serviceId)
        ? JSON.stringify({ version: 1, sessionIds: ["stale"] })
        : null,
      setItem: () => {},
    };
    const api = client(async (input, init) => {
      const path = new URL(input instanceof Request ? input.url : input.toString()).pathname;
      if (path === "/api/health") {
        if (health === "failed") return new Response(null, { status: 503 });
        return Response.json({
          status: "ok",
          service_id: health === "valid" ? serviceId : "not-a-service-uuid",
        });
      }
      if (path === "/api/config") return Response.json(config);
      if (path === "/api/sessions" && init?.method === "GET") {
        return Response.json([session("stale", "Stale"), session("current", "Current")]);
      }
      if (path === "/api/sessions" && init?.method === "POST") {
        creates += 1;
        return Response.json(session("created", "Created"), { status: 201 });
      }
      return new Response(null, { status: 404 });
    }, true);
    render(<SessionsHarness api={api} storage={storage} />);

    expect(await screen.findByRole("status", { name: "Refresh error" }))
      .toHaveTextContent("identity could not be confirmed");
    expect(screen.getByRole("status", { name: "Config readiness" })).toHaveTextContent("none");
    expect(screen.getByRole("status", { name: "Session titles" })).toBeEmptyDOMElement();
    await user.click(screen.getByRole("button", { name: "Create" }));
    expect(creates).toBe(0);

    health = "invalid";
    await user.click(screen.getByRole("button", { name: "Refresh" }));
    expect(await screen.findByRole("status", { name: "Refresh error" }))
      .toHaveTextContent("identity could not be confirmed");
    expect(screen.getByRole("status", { name: "Config readiness" })).toHaveTextContent("none");

    health = "valid";
    await user.click(screen.getByRole("button", { name: "Refresh" }));
    expect(await screen.findByText("Current", { selector: "output" })).toBeVisible();
    expect(screen.getByRole("status", { name: "Session titles" })).not.toHaveTextContent("Stale");
    expect(screen.getByRole("status", { name: "Config readiness" })).toHaveTextContent("ready");
    expect(screen.getByRole("status", { name: "Refresh error" })).toHaveTextContent("none");
  });

  it("persists failed superseded-create cleanup, suppresses the exact row, and retries only its DELETE", async () => {
    const user = userEvent.setup();
    const staleCreate = deferred<Response>();
    const values = new Map<string, string>();
    const storage: LedgerStorage = {
      getItem: (key) => values.get(key) ?? null,
      setItem: (key, value) => { values.set(key, value); },
    };
    const deletes: string[] = [];
    let archived = false;
    let staleExists = false;
    let healthAvailable = true;
    const api = client(async (input, init) => {
      const path = new URL(input instanceof Request ? input.url : input.toString()).pathname;
      if (path === "/api/health") {
        return healthAvailable
          ? Response.json({ status: "ok", service_id: "11111111-1111-4111-8111-111111111111" })
          : new Response(null, { status: 503 });
      }
      if (path === "/api/config") return Response.json(config);
      if (path === "/api/sessions" && init?.method === "GET") {
        return Response.json([
          ...(staleExists && !archived ? [session("stale", "Stale create")] : []),
          session("current", "Current"),
        ]);
      }
      if (path === "/api/sessions" && init?.method === "POST") return staleCreate.promise;
      if (path === "/api/sessions/current" && init?.method === "PATCH") {
        return Response.json(session("current", "Current renamed"));
      }
      if (init?.method === "DELETE") {
        deletes.push(path);
        if (deletes.length === 1) return new Response(null, { status: 503 });
        archived = true;
        return new Response(null, { status: 204 });
      }
      return new Response(null, { status: 404 });
    }, true);
    const rendered = render(<CleanupHarness api={api} storage={storage} />);
    await screen.findByText("Current", { selector: "output" });

    await user.click(screen.getByRole("button", { name: "Create stale" }));
    await user.click(screen.getByRole("button", { name: "Supersede" }));
    staleExists = true;
    staleCreate.resolve(Response.json(session("stale", "Stale create"), { status: 201 }));
    expect(await screen.findByRole("status", { name: "Cleanup status" }))
      .toHaveTextContent("cleanup:stale");
    expect(deletes).toEqual(["/api/sessions/stale"]);
    expect([...values.keys()].join("|")).not.toContain(connection.token);
    expect([...values.values()].join("|")).not.toContain("/work/stale");

    healthAvailable = false;
    await user.click(screen.getByRole("button", { name: "Refresh" }));
    await waitFor(() => expect(screen.getByRole("status", { name: "Session titles" }))
      .toHaveTextContent("Current"));
    expect(screen.getByRole("status", { name: "Session titles" })).not.toHaveTextContent("Stale create");
    expect(await screen.findByRole("status", { name: "Refresh error" }))
      .toHaveTextContent("identity could not be confirmed");
    healthAvailable = true;
    await user.click(screen.getByRole("button", { name: "Refresh" }));
    await waitFor(() => expect(screen.getByRole("status", { name: "Refresh error" }))
      .toHaveTextContent("none"));
    expect(screen.getByRole("status", { name: "Session titles" })).not.toHaveTextContent("Stale create");
    await user.click(screen.getByRole("button", { name: "Rename current" }));
    expect(await screen.findByText("Current renamed", { selector: "output" })).toBeVisible();
    expect(screen.getByRole("status", { name: "Cleanup status" })).toHaveTextContent("cleanup:stale");

    rendered.unmount();
    render(<CleanupHarness api={api} storage={storage} />);
    await screen.findByText("Current", { selector: "output" });
    expect(screen.getByRole("status", { name: "Session titles" })).not.toHaveTextContent("Stale create");
    expect(screen.getByRole("status", { name: "Cleanup status" })).toHaveTextContent("cleanup:stale");

    await user.dblClick(screen.getByRole("button", { name: "Retry cleanup" }));
    expect(deletes).toEqual(["/api/sessions/stale", "/api/sessions/stale"]);
    await waitFor(() => expect(screen.getByRole("status", { name: "Cleanup status" }))
      .toHaveTextContent("none"));
  });

  it("scopes cleanup suppression and late DELETE results to the stable service authority", async () => {
    const user = userEvent.setup();
    const staleCreate = deferred<Response>();
    const staleDelete = deferred<Response>();
    const values = new Map<string, string>();
    const storage: LedgerStorage = {
      getItem: (key) => values.get(key) ?? null,
      setItem: (key, value) => { values.set(key, value); },
    };
    const deleted: string[] = [];
    const serviceA = client(async (input, init) => {
      const path = new URL(input instanceof Request ? input.url : input.toString()).pathname;
      if (path === "/api/health") {
        return Response.json({ status: "ok", service_id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa" });
      }
      if (path === "/api/config") return Response.json(config);
      if (path === "/api/sessions" && init?.method === "GET") return Response.json([]);
      if (path === "/api/sessions" && init?.method === "POST") return staleCreate.promise;
      if (init?.method === "DELETE") {
        deleted.push(`A:${path}`);
        return staleDelete.promise;
      }
      return new Response(null, { status: 404 });
    }, true);
    const serviceB = client(async (input) => {
      const path = new URL(input instanceof Request ? input.url : input.toString()).pathname;
      if (path === "/api/health") {
        return Response.json({ status: "ok", service_id: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb" });
      }
      if (path === "/api/config") return Response.json(config);
      if (path === "/api/sessions") return Response.json([session("stale", "Current on B")]);
      return new Response(null, { status: 404 });
    }, true);
    const rendered = render(<CleanupHarness api={serviceA} storage={storage} />);
    await screen.findByText("ready", { selector: "output" });
    await user.click(screen.getByRole("button", { name: "Create stale" }));
    await user.click(screen.getByRole("button", { name: "Supersede" }));
    staleCreate.resolve(Response.json(session("stale", "Stale on A"), { status: 201 }));
    await waitFor(() => expect(deleted).toEqual(["A:/api/sessions/stale"]));

    rendered.rerender(<CleanupHarness api={serviceB} storage={storage} />);
    expect(await screen.findByText("Current on B", { selector: "output" })).toBeVisible();
    staleDelete.resolve(new Response(null, { status: 503 }));
    await act(async () => { await Promise.resolve(); });
    expect(screen.getByRole("status", { name: "Cleanup status" })).toHaveTextContent("none");
    expect(deleted).toEqual(["A:/api/sessions/stale"]);

    rendered.rerender(<CleanupHarness api={serviceA} storage={storage} />);
    await waitFor(() => expect(screen.getByRole("status", { name: "Cleanup status" }))
      .toHaveTextContent("cleanup:stale"));
    await user.click(screen.getByRole("button", { name: "Retry cleanup" }));
    expect(deleted).toEqual([
      "A:/api/sessions/stale",
      "A:/api/sessions/stale",
    ]);
  });

  it("preserves a typed rename failure apart from refresh readiness and retries its exact request", async () => {
    const user = userEvent.setup();
    const titles: string[] = [];
    const api = client(async (input, init) => {
      const path = new URL(input instanceof Request ? input.url : input.toString()).pathname;
      if (path === "/api/config") return Response.json(config);
      if (path === "/api/sessions" && init?.method === "GET") {
        return Response.json([session("session-1", "Original")]);
      }
      if (init?.method === "PATCH") {
        const body = JSON.parse(String(init.body)) as { title: string };
        titles.push(body.title);
        return titles.length === 1
          ? Response.json({ error: { type: "invalid_title", message: "Bad title" } }, { status: 422 })
          : Response.json(session("session-1", body.title));
      }
      return new Response(null, { status: 404 });
    });
    render(<SessionsHarness api={api} />);
    await screen.findByText("Original", { selector: "output" });

    await user.click(screen.getByRole("button", { name: "First rename" }));
    expect(await screen.findByRole("status", { name: "Operation error" }))
      .toHaveTextContent("rename:invalid_title");
    expect(screen.getByRole("status", { name: "Refresh error" })).toHaveTextContent("none");

    await user.dblClick(screen.getByRole("button", { name: "Retry operation" }));
    expect(titles).toEqual(["First rename", "First rename"]);
    expect(await screen.findByText("First rename", { selector: "output" })).toBeVisible();
    expect(screen.getByRole("status", { name: "Operation error" })).toHaveTextContent("none");
  });

  it("uses explicit validated workspace and defaults for a future session", async () => {
    const user = userEvent.setup();
    let createBody: unknown;
    const api = client(async (input, init) => {
      const path = new URL(input instanceof Request ? input.url : input.toString()).pathname;
      if (path === "/api/config") return Response.json(config);
      if (path === "/api/sessions" && init?.method === "GET") return Response.json([]);
      if (path === "/api/sessions" && init?.method === "POST") {
        createBody = JSON.parse(String(init.body));
        return Response.json(session("explicit", "Explicit chat"), { status: 201 });
      }
      return new Response(null, { status: 404 });
    });
    render(<SessionsHarness api={api} />);
    await waitFor(() => expect(screen.getByRole("button", { name: "Create explicit" })).toBeEnabled());

    await user.click(screen.getByRole("button", { name: "Create explicit" }));
    expect(await screen.findByText("Explicit chat", { selector: "output" })).toBeVisible();
    expect(createBody).toEqual({
      workspace: "/work/explicit",
      mode: "readOnly",
      context_mode: "folding",
      title: "Explicit chat",
    });
  });

  it("rejects invalid explicit options before dispatch", async () => {
    const user = userEvent.setup();
    let creates = 0;
    const api = client(async (input, init) => {
      const path = new URL(input instanceof Request ? input.url : input.toString()).pathname;
      if (path === "/api/config") return Response.json(config);
      if (path === "/api/sessions" && init?.method === "GET") return Response.json([]);
      if (path === "/api/sessions" && init?.method === "POST") creates += 1;
      return new Response(null, { status: 404 });
    });
    render(<SessionsHarness api={api} />);
    await user.click(await screen.findByRole("button", { name: "Create invalid" }));
    expect(creates).toBe(0);
    expect(screen.getByRole("status", { name: "Operation error" })).toHaveTextContent(
      "Invalid session creation options.",
    );
  });

  it("ignores a slow refresh from a superseded client", async () => {
    const oldSessions = deferred<Response>();
    const oldConfig = deferred<Response>();
    const oldClient = client(async (input) => {
      const path = new URL(input instanceof Request ? input.url : input.toString()).pathname;
      return path === "/api/sessions" ? oldSessions.promise : oldConfig.promise;
    });
    const newClient = client(async (input) => {
      const path = new URL(input instanceof Request ? input.url : input.toString()).pathname;
      return Response.json(path === "/api/sessions" ? [session("new-client")] : config);
    });

    const { rerender } = render(<SessionsHarness api={oldClient} />);
    rerender(<SessionsHarness api={newClient} />);
    expect(await screen.findByRole("status", { name: "Session titles" })).toHaveTextContent(
      "new-client",
    );

    oldSessions.resolve(Response.json([session("stale-client")]));
    oldConfig.resolve(Response.json(config));
    await waitFor(() =>
      expect(screen.getByRole("status", { name: "Session titles" })).toHaveTextContent(
        "new-client",
      ),
    );
    expect(screen.getByRole("status", { name: "Session titles" })).not.toHaveTextContent(
      "stale-client",
    );
  });

  it("does not let an older refresh drop a session created while it was pending", async () => {
    const user = userEvent.setup();
    const staleList = deferred<Response>();
    let listRequests = 0;
    const api = client(async (input, init) => {
      const path = new URL(input instanceof Request ? input.url : input.toString()).pathname;
      if (path === "/api/config") return Response.json(config);
      if (path === "/api/sessions" && init?.method === "GET") {
        listRequests += 1;
        return listRequests === 1
          ? Response.json([session("session-1")])
          : staleList.promise;
      }
      if (path === "/api/sessions" && init?.method === "POST") {
        return Response.json(session("created", "Created session"), { status: 201 });
      }
      return new Response(null, { status: 404 });
    });
    render(<SessionsHarness api={api} />);
    await waitFor(() =>
      expect(screen.getByRole("status", { name: "Session titles" })).toHaveTextContent(
        "session-1",
      ),
    );

    await user.click(screen.getByRole("button", { name: "Refresh" }));
    await user.click(screen.getByRole("button", { name: "Create" }));
    expect(await screen.findByText(/Created session/, { selector: "output" })).toBeVisible();
    staleList.resolve(Response.json([session("session-1")]));

    await waitFor(() =>
      expect(screen.getByRole("status", { name: "Session titles" })).toHaveTextContent(
        "Created session",
      ),
    );
  });

  it("keeps the latest rapid rename when responses finish out of order", async () => {
    const user = userEvent.setup();
    const first = deferred<Response>();
    const second = deferred<Response>();
    const api = client(async (input, init) => {
      const path = new URL(input instanceof Request ? input.url : input.toString()).pathname;
      if (path === "/api/config") return Response.json(config);
      if (path === "/api/sessions" && init?.method === "GET") {
        return Response.json([session("session-1", "Original")]);
      }
      if (init?.method === "PATCH") {
        const body = JSON.parse(String(init.body)) as { title: string };
        return body.title === "First rename" ? first.promise : second.promise;
      }
      return new Response(null, { status: 404 });
    });
    render(<SessionsHarness api={api} />);
    await screen.findByText("Original", { selector: "output" });

    await user.click(screen.getByRole("button", { name: "First rename" }));
    await user.click(screen.getByRole("button", { name: "Second rename" }));
    second.resolve(Response.json(session("session-1", "Second rename")));
    expect(await screen.findByText("Second rename", { selector: "output" })).toBeVisible();
    first.resolve(Response.json(session("session-1", "First rename")));

    await waitFor(() =>
      expect(screen.getByRole("status", { name: "Session titles" })).toHaveTextContent(
        "Second rename",
      ),
    );
    expect(screen.getByRole("status", { name: "Session titles" })).not.toHaveTextContent(
      "First rename",
    );
  });

  it("does not let a pending refresh resurrect a completed archive", async () => {
    const user = userEvent.setup();
    const staleList = deferred<Response>();
    let listRequests = 0;
    const api = client(async (input, init) => {
      const path = new URL(input instanceof Request ? input.url : input.toString()).pathname;
      if (path === "/api/config") return Response.json(config);
      if (path === "/api/sessions" && init?.method === "GET") {
        listRequests += 1;
        return listRequests === 1
          ? Response.json([session("session-1")])
          : staleList.promise;
      }
      if (init?.method === "DELETE") return new Response(null, { status: 204 });
      return new Response(null, { status: 404 });
    });
    render(<SessionsHarness api={api} />);
    await waitFor(() =>
      expect(screen.getByRole("status", { name: "Session titles" })).toHaveTextContent(
        "session-1",
      ),
    );

    await user.click(screen.getByRole("button", { name: "Refresh" }));
    await user.click(screen.getByRole("button", { name: "Archive" }));
    await waitFor(() =>
      expect(screen.getByRole("status", { name: "Session titles" })).toBeEmptyDOMElement(),
    );
    staleList.resolve(Response.json([session("session-1")]));

    await waitFor(() =>
      expect(screen.getByRole("status", { name: "Session titles" })).not.toHaveTextContent(
        "session-1",
      ),
    );
    expect(screen.getByRole("status", { name: "Active session" })).toHaveTextContent("none");
  });

  it("keeps an earlier confirmed rename when a later rename fails", async () => {
    const user = userEvent.setup();
    const first = deferred<Response>();
    const second = deferred<Response>();
    let patches = 0;
    const api = client(async (input, init) => {
      const path = new URL(input instanceof Request ? input.url : input.toString()).pathname;
      if (path === "/api/config") return Response.json(config);
      if (path === "/api/sessions" && init?.method === "GET") {
        return Response.json([session("session-1", "Original")]);
      }
      if (init?.method === "PATCH") {
        patches += 1;
        return patches === 1 ? first.promise : second.promise;
      }
      return new Response(null, { status: 404 });
    });
    render(<SessionsHarness api={api} />);
    await screen.findByText("Original", { selector: "output" });

    await user.click(screen.getByRole("button", { name: "First rename" }));
    await user.click(screen.getByRole("button", { name: "Second rename" }));
    first.resolve(Response.json(session("session-1", "First confirmed")));
    expect(await screen.findByText("First confirmed", { selector: "output" })).toBeVisible();
    second.resolve(new Response(null, { status: 500 }));

    await waitFor(() =>
      expect(screen.getByRole("status", { name: "Operation error" })).not.toHaveTextContent("none"),
    );
    expect(screen.getByRole("status", { name: "Session titles" })).toHaveTextContent(
      "First confirmed",
    );
  });

  it("applies an earlier confirmed duplicate archive when the later archive fails", async () => {
    const user = userEvent.setup();
    const first = deferred<Response>();
    const second = deferred<Response>();
    let deletes = 0;
    const api = client(async (input, init) => {
      const path = new URL(input instanceof Request ? input.url : input.toString()).pathname;
      if (path === "/api/config") return Response.json(config);
      if (path === "/api/sessions" && init?.method === "GET") {
        return Response.json([session("session-1")]);
      }
      if (init?.method === "DELETE") {
        deletes += 1;
        return deletes === 1 ? first.promise : second.promise;
      }
      return new Response(null, { status: 404 });
    });
    render(<SessionsHarness api={api} />);
    await waitFor(() =>
      expect(screen.getByRole("status", { name: "Session titles" })).toHaveTextContent(
        "session-1",
      ),
    );

    await user.click(screen.getByRole("button", { name: "Archive" }));
    await user.click(screen.getByRole("button", { name: "Archive" }));
    first.resolve(new Response(null, { status: 204 }));
    await waitFor(() =>
      expect(screen.getByRole("status", { name: "Session titles" })).toBeEmptyDOMElement(),
    );
    await act(async () => {
      second.resolve(new Response(null, { status: 404 }));
    });

    await waitFor(() =>
      expect(screen.getByRole("status", { name: "Active session" })).toHaveTextContent("none"),
    );
    expect(screen.getByRole("status", { name: "Session titles" })).toBeEmptyDOMElement();
  });

  it("keeps a confirmed rename when a later cross-kind archive fails", async () => {
    const user = userEvent.setup();
    const rename = deferred<Response>();
    const archive = deferred<Response>();
    const api = client(async (input, init) => {
      const path = new URL(input instanceof Request ? input.url : input.toString()).pathname;
      if (path === "/api/config") return Response.json(config);
      if (path === "/api/sessions" && init?.method === "GET") {
        return Response.json([session("session-1", "Original")]);
      }
      if (init?.method === "PATCH") return rename.promise;
      if (init?.method === "DELETE") return archive.promise;
      return new Response(null, { status: 404 });
    });
    render(<SessionsHarness api={api} />);
    await screen.findByText("Original", { selector: "output" });

    await user.click(screen.getByRole("button", { name: "First rename" }));
    await user.click(screen.getByRole("button", { name: "Archive" }));
    rename.resolve(Response.json(session("session-1", "Confirmed rename")));
    expect(await screen.findByText("Confirmed rename", { selector: "output" })).toBeVisible();
    archive.resolve(new Response(null, { status: 500 }));

    await waitFor(() =>
      expect(screen.getByRole("status", { name: "Operation error" })).not.toHaveTextContent("none"),
    );
    expect(screen.getByRole("status", { name: "Session titles" })).toHaveTextContent(
      "Confirmed rename",
    );
  });

  it("lets a confirmed archive dominate a later stale rename success", async () => {
    const user = userEvent.setup();
    const archive = deferred<Response>();
    const rename = deferred<Response>();
    const api = client(async (input, init) => {
      const path = new URL(input instanceof Request ? input.url : input.toString()).pathname;
      if (path === "/api/config") return Response.json(config);
      if (path === "/api/sessions" && init?.method === "GET") {
        return Response.json([session("session-1", "Original")]);
      }
      if (init?.method === "DELETE") return archive.promise;
      if (init?.method === "PATCH") return rename.promise;
      return new Response(null, { status: 404 });
    });
    render(<SessionsHarness api={api} />);
    await screen.findByText("Original", { selector: "output" });

    await user.click(screen.getByRole("button", { name: "Archive" }));
    await user.click(screen.getByRole("button", { name: "First rename" }));
    archive.resolve(new Response(null, { status: 204 }));
    await waitFor(() =>
      expect(screen.getByRole("status", { name: "Session titles" })).toBeEmptyDOMElement(),
    );
    await act(async () => {
      rename.resolve(Response.json(session("session-1", "Stale rename")));
    });

    await waitFor(() =>
      expect(screen.getByRole("status", { name: "Session titles" })).not.toHaveTextContent(
        "Stale rename",
      ),
    );
    expect(screen.getByRole("status", { name: "Active session" })).toHaveTextContent("none");
  });

  it("does not let an older concurrent create response steal selection", async () => {
    const user = userEvent.setup();
    const first = deferred<Response>();
    const second = deferred<Response>();
    let creates = 0;
    const api = client(async (input, init) => {
      const path = new URL(input instanceof Request ? input.url : input.toString()).pathname;
      if (path === "/api/config") return Response.json(config);
      if (path === "/api/sessions" && init?.method === "GET") {
        return Response.json([session("session-1")]);
      }
      if (path === "/api/sessions" && init?.method === "POST") {
        creates += 1;
        return creates === 1 ? first.promise : second.promise;
      }
      return new Response(null, { status: 404 });
    });
    render(<SessionsHarness api={api} />);
    await waitFor(() =>
      expect(screen.getByRole("status", { name: "Session titles" })).toHaveTextContent(
        "session-1",
      ),
    );

    await user.click(screen.getByRole("button", { name: "Create" }));
    await user.click(screen.getByRole("button", { name: "Create" }));
    second.resolve(Response.json(session("created-2", "Newer create"), { status: 201 }));
    await waitFor(() =>
      expect(screen.getByRole("status", { name: "Active session" })).toHaveTextContent(
        "created-2",
      ),
    );
    first.resolve(Response.json(session("created-1", "Older create"), { status: 201 }));

    await waitFor(() =>
      expect(screen.getByRole("status", { name: "Session titles" })).toHaveTextContent(
        "Older create",
      ),
    );
    expect(screen.getByRole("status", { name: "Active session" })).toHaveTextContent(
      "created-2",
    );
  });

  it("preserves a later explicit selection while create is pending", async () => {
    const user = userEvent.setup();
    const create = deferred<Response>();
    const api = client(async (input, init) => {
      const path = new URL(input instanceof Request ? input.url : input.toString()).pathname;
      if (path === "/api/config") return Response.json(config);
      if (path === "/api/sessions" && init?.method === "GET") {
        return Response.json([session("session-1"), session("session-2")]);
      }
      if (path === "/api/sessions" && init?.method === "POST") return create.promise;
      return new Response(null, { status: 404 });
    });
    render(<SessionsHarness api={api} />);
    await screen.findByText("session-1|session-2", { selector: "output" });

    await user.click(screen.getByRole("button", { name: "Create" }));
    await user.click(screen.getByRole("button", { name: "Select session-2" }));
    expect(screen.getByRole("status", { name: "Active session" })).toHaveTextContent("session-2");
    create.resolve(Response.json(session("created", "Created later"), { status: 201 }));

    await waitFor(() =>
      expect(screen.getByRole("status", { name: "Session titles" })).toHaveTextContent(
        "Created later",
      ),
    );
    expect(screen.getByRole("status", { name: "Active session" })).toHaveTextContent("session-2");
  });
});
