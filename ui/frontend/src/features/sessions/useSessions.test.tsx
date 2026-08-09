import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it } from "vitest";

import { ApiClient } from "../../api/http";
import type { SessionRecord } from "./useSessions";
import { useSessions } from "./useSessions";

const connection = {
  baseUrl: "http://127.0.0.1:4010",
  token: "t".repeat(43),
};

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

function client(fetchRequest: typeof fetch) {
  return new ApiClient(connection, fetchRequest);
}

function SessionsHarness({ api }: { api: ApiClient }) {
  const model = useSessions(api);
  return (
    <>
      <output aria-label="Session titles">{model.sessions.map((item) => item.title).join("|")}</output>
      <output aria-label="Active session">{model.activeSessionId ?? "none"}</output>
      <output aria-label="Session error">{model.error?.message ?? "none"}</output>
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
    </>
  );
}

afterEach(cleanup);

describe("useSessions async authority", () => {
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
    expect(screen.getByRole("status", { name: "Session error" })).toHaveTextContent(
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
      expect(screen.getByRole("status", { name: "Session error" })).not.toHaveTextContent("none"),
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
      expect(screen.getByRole("status", { name: "Session error" })).not.toHaveTextContent("none"),
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
