import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { App } from "./App";
import { ApiClient } from "./api/http";
import type { PlatformAdapter } from "./platform/types";
import type { SessionRecord } from "./features/sessions/useSessions";
import { event } from "./protocol/fixtures";
import { emptyTranscript, transcriptReducer } from "./protocol/reducer";
import type { ClientEvent } from "./protocol/types";

const connection = {
  baseUrl: "http://127.0.0.1:4010",
  token: "t".repeat(43),
};
const testServiceId = "11111111-1111-4111-8111-111111111111";

const storage = {
  getItem: () => null,
  setItem: () => {},
};

function platformAdapter(changes: Partial<PlatformAdapter> = {}): PlatformAdapter {
  return {
    kind: "browser",
    getServiceConnection: async () => connection,
    chooseWorkspace: async () => null,
    notify: async () => {},
    ...changes,
  };
}

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

function deferred<T = void>() {
  let resolve!: (value: T | PromiseLike<T>) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((accept, decline) => {
    resolve = accept;
    reject = decline;
  });
  return { promise, resolve, reject };
}

function capturedSubmissionId(
  spy: { readonly mock: { readonly calls: readonly (readonly unknown[])[] } },
  callIndex: number,
): string {
  const outbound = spy.mock.calls[callIndex]?.[1] as { readonly submission_id?: unknown } | undefined;
  if (typeof outbound?.submission_id !== "string") {
    throw new Error(`call ${callIndex} did not carry a submission id`);
  }
  return outbound.submission_id;
}

function clientWith(fetchRequest: typeof fetch) {
  const withIdentity: typeof fetch = async (input, init) => {
    const path = new URL(input instanceof Request ? input.url : input.toString()).pathname;
    if (path === "/api/health") {
      return Response.json({ status: "ok", service_id: testServiceId });
    }
    return fetchRequest(input, init);
  };
  return new ApiClient(connection, withIdentity);
}

function clientWithSessions(records: readonly SessionRecord[]) {
  const fetchRequest: typeof fetch = async (input, init) => {
    const url = new URL(input instanceof Request ? input.url : input.toString());
    if (url.pathname === "/api/config") {
      return Response.json({
        base_workspace: "/work/default",
        default_mode: "default",
        default_context_mode: "compaction",
        modes: ["default", "acceptAll", "readOnly"],
        context_modes: ["compaction", "folding"],
      });
    }
    if (url.pathname === "/api/sessions" && init?.method === "GET") {
      return Response.json(records);
    }
    return new Response(null, { status: 404 });
  };
  return clientWith(fetchRequest);
}

async function selectSession(user: ReturnType<typeof userEvent.setup>, title: string) {
  await user.click(await screen.findByRole("button", { name: new RegExp(`^${title},`, "i") }));
  await waitFor(() =>
    expect(screen.getByRole("button", { name: new RegExp(`^${title},`, "i") }))
      .toHaveAttribute("aria-current", "page"),
  );
}

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("App", () => {
  it("projects a direct prompt immediately, keeps it across session switches, and reconciles completion once", async () => {
    const user = userEvent.setup();
    const sessionA = session({ session_id: "optimistic-a", title: "Optimistic A" });
    const sessionB = session({ session_id: "optimistic-b", title: "Optimistic B" });
    const onSessionEvent = vi.fn();
    const props = {
      client: clientWithSessions([sessionA, sessionB]),
      platformAdapter: platformAdapter(),
      sidebarStorage: storage,
      draftStorage: storage,
      onSessionEvent,
    };
    const initial = {
      [sessionA.session_id]: emptyTranscript(),
      [sessionB.session_id]: emptyTranscript(),
    };
    const { rerender } = render(<App {...props} transcriptBySession={initial} />);

    await user.type(await screen.findByRole("textbox", { name: "Message" }), "Visible before streaming");
    await user.click(screen.getByRole("button", { name: "Send message" }));
    expect(screen.getByRole("article", { name: "User message" })).toHaveTextContent("Visible before streaming");

    await selectSession(user, sessionB.title);
    expect(screen.queryByText("Visible before streaming", { exact: true })).not.toBeInTheDocument();
    await selectSession(user, sessionA.title);
    expect(screen.getByRole("article", { name: "User message" })).toHaveTextContent("Visible before streaming");

    const submissionId = capturedSubmissionId(onSessionEvent, 0);
    let state = transcriptReducer(emptyTranscript(), event("turn_started", {
      sequence: 1,
      turn_id: "optimistic-turn",
      submission_id: submissionId,
    }));
    state = transcriptReducer(state, event("assistant_delta", {
      sequence: 2,
      turn_id: "optimistic-turn",
      text: "Streaming after prompt",
    }));
    rerender(<App {...props} transcriptBySession={{ ...initial, [sessionA.session_id]: state }} />);
    const prompt = screen.getByRole("article", { name: "User message" });
    const streaming = screen.getByRole("article", { name: "Assistant message" });
    expect(prompt.compareDocumentPosition(streaming) & Node.DOCUMENT_POSITION_FOLLOWING).not.toBe(0);

    state = transcriptReducer(state, event("turn_completed", {
      sequence: 3,
      turn_id: "optimistic-turn",
      messages: [
        { role: "user", content: "Visible before streaming" },
        { role: "assistant", content: "Complete" },
      ],
      final_text: "Complete",
    }));
    rerender(<App {...props} transcriptBySession={{ ...initial, [sessionA.session_id]: state }} />);
    expect(screen.getAllByRole("article", { name: "User message" })).toHaveLength(1);
  });

  it("restores an exact failed direct prompt", async () => {
    const user = userEvent.setup();
    const active = session({ session_id: "restore-failed", title: "Restore failed" });
    const onSessionEvent = vi.fn();
    const props = {
      client: clientWithSessions([active]),
      platformAdapter: platformAdapter(),
      sidebarStorage: storage,
      draftStorage: storage,
      onSessionEvent,
    };
    const { rerender } = render(
      <App {...props} transcriptBySession={{ [active.session_id]: emptyTranscript() }} />,
    );
    const textbox = await screen.findByRole("textbox", { name: "Message" });
    await user.type(textbox, "Restore this exact prompt");
    await user.click(screen.getByRole("button", { name: "Send message" }));
    await waitFor(() => expect(textbox).toHaveValue(""));
    const submissionId = capturedSubmissionId(onSessionEvent, 0);
    let state = transcriptReducer(emptyTranscript(), event("turn_started", {
      sequence: 1,
      turn_id: "restore-turn",
      submission_id: submissionId,
    }));
    state = transcriptReducer(state, event("turn_failed", {
      sequence: 2,
      turn_id: "restore-turn",
      error_category: "turn_failure",
    }));
    rerender(<App {...props} transcriptBySession={{ [active.session_id]: state }} />);
    expect(await screen.findByRole("textbox", { name: "Message" })).toHaveValue("Restore this exact prompt");
  });

  it("does not let failed prompt restoration overwrite a newer draft", async () => {
    const user = userEvent.setup();
    const active = session({ session_id: "keep-newer", title: "Keep newer" });
    const onSessionEvent = vi.fn();
    const props = {
      client: clientWithSessions([active]),
      platformAdapter: platformAdapter(),
      sidebarStorage: storage,
      draftStorage: storage,
      onSessionEvent,
    };
    const { rerender } = render(
      <App {...props} transcriptBySession={{ [active.session_id]: emptyTranscript() }} />,
    );
    const textbox = await screen.findByRole("textbox", { name: "Message" });
    await user.type(textbox, "Older failed prompt");
    await user.click(screen.getByRole("button", { name: "Send message" }));
    await waitFor(() => expect(textbox).toHaveValue(""));
    await user.type(textbox, "Keep my newer draft");
    const submissionId = capturedSubmissionId(onSessionEvent, 0);
    let state = transcriptReducer(emptyTranscript(), event("turn_started", {
      sequence: 1,
      turn_id: "keep-newer-turn",
      submission_id: submissionId,
    }));
    state = transcriptReducer(state, event("turn_failed", {
      sequence: 2,
      turn_id: "keep-newer-turn",
      error_category: "turn_failure",
    }));
    rerender(<App {...props} transcriptBySession={{ [active.session_id]: state }} />);
    expect(screen.getByRole("textbox", { name: "Message" })).toHaveValue("Keep my newer draft");
  });

  it("creates the first real session from onboarding with persisted future defaults exactly once", async () => {
    const user = userEvent.setup();
    const values = new Map<string, string>([[
      "agent-harness:preferences",
      JSON.stringify({
        version: 1,
        appearance: "system",
        defaultMode: "readOnly",
        contextMode: "folding",
        notifyPermissions: false,
        notifyCompletions: false,
        sidebarCollapsed: false,
      }),
    ]]);
    const preferenceStorage = {
      getItem: (key: string) => values.get(key) ?? null,
      setItem: (key: string, value: string) => { values.set(key, value); },
    };
    const createBodies: unknown[] = [];
    const fetchRequest: typeof fetch = async (input, init) => {
      const url = new URL(input instanceof Request ? input.url : input.toString());
      if (url.pathname === "/api/config") return Response.json({
        base_workspace: "/work/base",
        default_mode: "default",
        default_context_mode: "compaction",
        modes: ["default", "acceptAll", "readOnly"],
        context_modes: ["compaction", "folding"],
      });
      if (url.pathname === "/api/sessions" && init?.method === "GET") return Response.json([]);
      if (url.pathname === "/api/sessions" && init?.method === "POST") {
        createBodies.push(JSON.parse(String(init.body)));
        return Response.json(session({
          session_id: "first-real",
          workspace: "/work/first",
          title: "New chat",
          mode: "readOnly",
          context_mode: "folding",
        }), { status: 201 });
      }
      return new Response(null, { status: 404 });
    };
    render(
      <App
        client={clientWith(fetchRequest)}
        platformAdapter={platformAdapter()}
        preferenceStorage={preferenceStorage}
        sidebarStorage={storage}
        draftStorage={storage}
      />,
    );

    const path = await screen.findByRole("textbox", { name: "Workspace path" });
    await user.clear(path);
    await user.type(path, "/work/first");
    await user.dblClick(screen.getByRole("button", { name: "Start local session" }));
    expect(createBodies).toEqual([{
      workspace: "/work/first",
      mode: "readOnly",
      context_mode: "folding",
      title: "New chat",
    }]);
    expect(await screen.findByRole("heading", { name: "first" })).toBeVisible();
    expect(screen.queryByRole("heading", { name: "Start locally" })).not.toBeInTheDocument();
  });

  it("lets newer first-run choices supersede a pending successful create and cleans up only its exact stale session", async () => {
    const user = userEvent.setup();
    const stale = deferred<Response>();
    const deleted: string[] = [];
    let posts = 0;
    const fetchRequest: typeof fetch = async (input, init) => {
      const path = new URL(input instanceof Request ? input.url : input.toString()).pathname;
      if (path === "/api/config") return Response.json({
        base_workspace: "/work/old",
        default_mode: "default",
        default_context_mode: "compaction",
        modes: ["default", "acceptAll", "readOnly"],
        context_modes: ["compaction", "folding"],
      });
      if (path === "/api/sessions" && init?.method === "GET") return Response.json([]);
      if (path === "/api/sessions" && init?.method === "POST") {
        posts += 1;
        return posts === 1
          ? stale.promise
          : Response.json(session({
              session_id: "new-choice",
              workspace: "/work/new",
              title: "New choice",
              mode: "readOnly",
              context_mode: "folding",
            }), { status: 201 });
      }
      if (init?.method === "DELETE") {
        deleted.push(path);
        return new Response(null, { status: 204 });
      }
      return new Response(null, { status: 404 });
    };
    render(
      <App
        client={clientWith(fetchRequest)}
        platformAdapter={platformAdapter()}
        sidebarStorage={storage}
        draftStorage={storage}
      />,
    );

    const path = await screen.findByRole("textbox", { name: "Workspace path" });
    await user.click(screen.getByRole("button", { name: "Start local session" }));
    expect(posts).toBe(1);
    await user.clear(path);
    await user.type(path, "/work/new");
    await user.selectOptions(screen.getByRole("combobox", { name: "Permission mode" }), "readOnly");
    await user.click(screen.getByRole("radio", { name: "Recoverable folding" }));
    expect(screen.getByRole("button", { name: "Start local session" })).toBeEnabled();
    await user.click(screen.getByRole("button", { name: "Start local session" }));
    expect(posts).toBe(2);
    expect(await screen.findByRole("heading", { name: "new" })).toBeVisible();

    stale.resolve(Response.json(session({
      session_id: "stale-old",
      workspace: "/work/old",
      title: "Stale old",
    }), { status: 201 }));
    await waitFor(() => expect(deleted).toEqual(["/api/sessions/stale-old"]));
    expect(screen.queryByRole("button", { name: /^Stale old,/ })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^New choice,/ })).toHaveAttribute("aria-current", "page");
  });

  it("contains a stale first-run failure without blocking or replacing the newer choices", async () => {
    const user = userEvent.setup();
    const stale = deferred<Response>();
    let posts = 0;
    const fetchRequest: typeof fetch = async (input, init) => {
      const path = new URL(input instanceof Request ? input.url : input.toString()).pathname;
      if (path === "/api/config") return Response.json({
        base_workspace: "/work/old",
        default_mode: "default",
        default_context_mode: "compaction",
        modes: ["default", "acceptAll", "readOnly"],
        context_modes: ["compaction", "folding"],
      });
      if (path === "/api/sessions" && init?.method === "GET") return Response.json([]);
      if (path === "/api/sessions" && init?.method === "POST") {
        posts += 1;
        return posts === 1
          ? stale.promise
          : Response.json(session({
              session_id: "new-after-failure",
              workspace: "/work/newer",
              title: "Newer choice",
              mode: "acceptAll",
              context_mode: "folding",
            }), { status: 201 });
      }
      return new Response(null, { status: 404 });
    };
    render(
      <App
        client={clientWith(fetchRequest)}
        platformAdapter={platformAdapter()}
        sidebarStorage={storage}
        draftStorage={storage}
      />,
    );

    const path = await screen.findByRole("textbox", { name: "Workspace path" });
    await user.click(screen.getByRole("button", { name: "Start local session" }));
    await user.clear(path);
    await user.type(path, "/work/newer");
    await user.selectOptions(screen.getByRole("combobox", { name: "Permission mode" }), "acceptAll");
    await user.click(screen.getByRole("radio", { name: "Recoverable folding" }));
    await user.click(screen.getByRole("button", { name: "Start local session" }));
    expect(await screen.findByRole("heading", { name: "newer" })).toBeVisible();

    stale.resolve(Response.json({
      error: { type: "credential_prerequisite", message: "stale credential requirement" },
    }, { status: 409 }));
    await act(async () => { await Promise.resolve(); });
    expect(screen.queryByRole("heading", { name: "Sign in required" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^Newer choice,/ })).toHaveAttribute("aria-current", "page");
  });

  it("bypasses first run, opens internal Settings from sidebar and palette, and applies defaults only to later New chat", async () => {
    const user = userEvent.setup();
    const onSessionEvent = vi.fn();
    const createBodies: unknown[] = [];
    const fetchRequest: typeof fetch = async (input, init) => {
      const url = new URL(input instanceof Request ? input.url : input.toString());
      if (url.pathname === "/api/config") return Response.json({
        base_workspace: "/work/base",
        default_mode: "default",
        default_context_mode: "compaction",
        modes: ["default", "acceptAll", "readOnly"],
        context_modes: ["compaction", "folding"],
      });
      if (url.pathname === "/api/sessions" && init?.method === "GET") return Response.json([session()]);
      if (url.pathname === "/api/sessions" && init?.method === "POST") {
        createBodies.push(JSON.parse(String(init.body)));
        return Response.json(session({ session_id: "future", workspace: "/work/base", title: "New chat", mode: "acceptAll", context_mode: "folding" }), { status: 201 });
      }
      return new Response(null, { status: 404 });
    };
    render(
      <App
        client={clientWith(fetchRequest)}
        platformAdapter={platformAdapter()}
        preferenceStorage={storage}
        sidebarStorage={storage}
        draftStorage={storage}
        transcriptBySession={{ "session-a": emptyTranscript() }}
        onSessionEvent={onSessionEvent}
      />,
    );
    expect(await screen.findByRole("heading", { name: "project-a" })).toBeVisible();
    expect(screen.queryByRole("heading", { name: "Start locally" })).not.toBeInTheDocument();
    const navigation = screen.getByRole("navigation", { name: "Sessions" });

    await user.click(screen.getByRole("button", { name: "Settings" }));
    await user.selectOptions(screen.getByRole("combobox", { name: "Default permission mode" }), "acceptAll");
    await user.click(screen.getByRole("radio", { name: "Recoverable folding" }));
    await user.click(screen.getByRole("checkbox", { name: "Collapse sidebar" }));
    expect(navigation).toHaveAttribute("data-collapsed", "true");
    await user.keyboard("{Escape}");
    expect(onSessionEvent).not.toHaveBeenCalled();

    await user.keyboard("{Meta>}k{/Meta}");
    await user.click(screen.getByRole("button", { name: "Open settings" }));
    expect(screen.getByRole("dialog", { name: "Settings" })).toBeVisible();
    await user.keyboard("{Escape}");
    await user.click(screen.getByRole("button", { name: "New chat" }));
    await waitFor(() => expect(createBodies).toHaveLength(1));
    expect(createBodies[0]).toEqual({
      workspace: "/work/base",
      mode: "acceptAll",
      context_mode: "folding",
      title: "New chat",
    });
    expect(onSessionEvent).not.toHaveBeenCalled();
  });

  it("projects any transcript turn failure through the stable turn recovery view", async () => {
    const active = session();
    render(
      <App
        client={clientWithSessions([active])}
        platformAdapter={platformAdapter()}
        sidebarStorage={storage}
        draftStorage={storage}
        transcriptBySession={{
          [active.session_id]: {
            ...emptyTranscript(),
            error: { category: "provider_internal_detail", message: "secret provider trace" },
          },
        }}
      />,
    );
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "The turn stopped before it completed.",
    );
    expect(screen.getByRole("alert")).not.toHaveTextContent("secret provider trace");
  });

  it("retries the exact retained failed submission once under session, client, generation, and turn authority", async () => {
    const user = userEvent.setup();
    const active = session();
    const client = clientWithSessions([active]);
    const retry = deferred();
    let attempts = 0;
    const onSessionEvent = vi.fn((_sessionId: string, event: ClientEvent) => {
      if (event.type !== "send_message") return;
      attempts += 1;
      return attempts === 2 ? retry.promise : undefined;
    });
    const appProps = {
      client,
      platformAdapter: platformAdapter(),
      sidebarStorage: storage,
      draftStorage: storage,
      onSessionEvent,
    };
    const { rerender } = render(
      <App {...appProps} transcriptBySession={{ [active.session_id]: emptyTranscript() }} />,
    );

    const textbox = await screen.findByRole("textbox", { name: "Message" });
    await user.type(textbox, "exact retained plan");
    await user.click(screen.getByRole("button", { name: "Plan mode" }));
    await user.click(screen.getByRole("button", { name: "Send message" }));
    const submission = capturedSubmissionId(onSessionEvent, 0);
    expect(onSessionEvent).toHaveBeenCalledWith(active.session_id, expect.objectContaining({
      type: "send_message",
      text: "exact retained plan",
      mode: "plan",
      submission_id: submission,
    }));

    const started = transcriptReducer(emptyTranscript(), event("turn_started", {
      sequence: 1,
      turn_id: "failed-turn",
      submission_id: submission,
    }));
    rerender(<App {...appProps} transcriptBySession={{ [active.session_id]: started }} />);
    await act(async () => { await Promise.resolve(); });
    const failed = transcriptReducer(started, event("turn_failed", {
      sequence: 2,
      turn_id: "failed-turn",
      error_category: "session_resume_error",
      message: "unsafe service detail",
    }));
    rerender(<App {...appProps} transcriptBySession={{ [active.session_id]: failed }} />);

    expect(await screen.findByRole("alert")).toHaveTextContent("This session could not be reopened.");
    await user.dblClick(screen.getByRole("button", { name: "Retry" }));
    expect(onSessionEvent).toHaveBeenCalledTimes(2);
    expect(onSessionEvent).toHaveBeenLastCalledWith(active.session_id, expect.objectContaining({
      type: "send_message",
      text: "exact retained plan",
      mode: "plan",
    }));
    await act(async () => retry.resolve());
  });

  it("binds an accepted retry to its new turn and offers the same exact retry after a second failure", async () => {
    const user = userEvent.setup();
    const active = session();
    const client = clientWithSessions([active]);
    const onSessionEvent = vi.fn();
    const props = { client, platformAdapter: platformAdapter(), sidebarStorage: storage, draftStorage: storage, onSessionEvent };
    const { rerender } = render(
      <App {...props} transcriptBySession={{ [active.session_id]: emptyTranscript() }} />,
    );
    await user.type(await screen.findByRole("textbox", { name: "Message" }), "same safe retry");
    await user.click(screen.getByRole("button", { name: "Send message" }));
    const initialSubmission = capturedSubmissionId(onSessionEvent, 0);
    let state = transcriptReducer(
      emptyTranscript(),
      event("turn_started", {
        sequence: 1,
        turn_id: "turn-a",
        submission_id: initialSubmission,
      }),
    );
    rerender(<App {...props} transcriptBySession={{ [active.session_id]: state }} />);
    await act(async () => { await Promise.resolve(); });
    state = transcriptReducer(state, event("turn_failed", { sequence: 2, turn_id: "turn-a" }));
    rerender(<App {...props} transcriptBySession={{ [active.session_id]: state }} />);

    await user.dblClick(await screen.findByRole("button", { name: "Retry" }));
    expect(onSessionEvent).toHaveBeenCalledTimes(2);
    const retrySubmission = capturedSubmissionId(onSessionEvent, 1);
    state = transcriptReducer(state, event("turn_started", {
      sequence: 3,
      turn_id: "turn-b",
      submission_id: retrySubmission,
    }));
    rerender(<App {...props} transcriptBySession={{ [active.session_id]: state }} />);
    await act(async () => { await Promise.resolve(); });
    state = transcriptReducer(state, event("turn_failed", { sequence: 4, turn_id: "turn-b" }));
    rerender(<App {...props} transcriptBySession={{ [active.session_id]: state }} />);

    await user.dblClick(await screen.findByRole("button", { name: "Retry" }));
    expect(onSessionEvent).toHaveBeenCalledTimes(3);
    expect(onSessionEvent).toHaveBeenLastCalledWith(active.session_id, expect.objectContaining({
      type: "send_message",
      text: "same safe retry",
      mode: "base",
    }));
  });

  it("assigns a fresh bounded submission id to direct, queued, and retry attempts", async () => {
    const user = userEvent.setup();
    const active = session();
    const client = clientWithSessions([active]);
    const onSessionEvent = vi.fn();
    const props = { client, platformAdapter: platformAdapter(), sidebarStorage: storage, draftStorage: storage, onSessionEvent };
    const { rerender } = render(<App {...props} transcriptBySession={{ [active.session_id]: emptyTranscript() }} />);
    await user.type(await screen.findByRole("textbox", { name: "Message" }), "turn A");
    await user.click(screen.getByRole("button", { name: "Send message" }));
    const submissionA = capturedSubmissionId(onSessionEvent, 0);
    expect(submissionA).toMatch(/^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/);
    let state = transcriptReducer(emptyTranscript(), event("turn_started", {
      sequence: 1,
      turn_id: "turn-a",
      submission_id: submissionA,
    }));
    rerender(<App {...props} transcriptBySession={{ [active.session_id]: state }} />);
    await act(async () => { await Promise.resolve(); });
    await user.type(screen.getByRole("textbox", { name: "Message" }), "queued B");
    await user.click(screen.getByRole("button", { name: "Queue message" }));
    const submissionB = capturedSubmissionId(onSessionEvent, 1);
    expect(submissionB).not.toBe(submissionA);
    state = transcriptReducer(state, event("turn_failed", { sequence: 2, turn_id: "turn-a" }));
    rerender(<App {...props} transcriptBySession={{ [active.session_id]: state }} />);

    await user.dblClick(await screen.findByRole("button", { name: "Retry" }));
    expect(onSessionEvent).toHaveBeenCalledTimes(3);
    const retrySubmission = capturedSubmissionId(onSessionEvent, 2);
    expect(new Set([submissionA, submissionB, retrySubmission])).toHaveLength(3);
    expect(onSessionEvent).toHaveBeenLastCalledWith(active.session_id, expect.objectContaining({
      type: "send_message",
      text: "turn A",
      mode: "base",
      submission_id: retrySubmission,
    }));
  });

  it("binds a queued follow-up only when its own turn starts and retries that failed follow-up", async () => {
    const user = userEvent.setup();
    const active = session();
    const client = clientWithSessions([active]);
    const onSessionEvent = vi.fn();
    const props = { client, platformAdapter: platformAdapter(), sidebarStorage: storage, draftStorage: storage, onSessionEvent };
    const { rerender } = render(
      <App {...props} transcriptBySession={{ [active.session_id]: emptyTranscript() }} />,
    );

    const textbox = await screen.findByRole("textbox", { name: "Message" });
    await user.type(textbox, "turn A");
    await user.click(screen.getByRole("button", { name: "Send message" }));
    const submissionA = capturedSubmissionId(onSessionEvent, 0);
    let state = transcriptReducer(emptyTranscript(), event("turn_started", {
      sequence: 1,
      turn_id: "turn-a",
      submission_id: submissionA,
    }));
    rerender(<App {...props} transcriptBySession={{ [active.session_id]: state }} />);
    await act(async () => { await Promise.resolve(); });
    await user.type(screen.getByRole("textbox", { name: "Message" }), "queued B");
    await user.click(screen.getByRole("button", { name: "Queue message" }));
    const submissionB = capturedSubmissionId(onSessionEvent, 1);

    state = transcriptReducer(state, event("turn_completed", { sequence: 2, turn_id: "turn-a" }));
    state = transcriptReducer(state, event("turn_started", {
      sequence: 3,
      turn_id: "turn-b",
      submission_id: submissionB,
    }));
    rerender(<App {...props} transcriptBySession={{ [active.session_id]: state }} />);
    await act(async () => { await Promise.resolve(); });
    state = transcriptReducer(state, event("turn_failed", { sequence: 4, turn_id: "turn-b" }));
    rerender(<App {...props} transcriptBySession={{ [active.session_id]: state }} />);

    await user.dblClick(await screen.findByRole("button", { name: "Retry" }));
    expect(onSessionEvent).toHaveBeenLastCalledWith(active.session_id, expect.objectContaining({
      type: "send_message",
      text: "queued B",
      mode: "base",
    }));
    expect(onSessionEvent).toHaveBeenCalledTimes(3);
  });

  it("retries direct turn A rather than queued B when A itself fails", async () => {
    const user = userEvent.setup();
    const active = session();
    const client = clientWithSessions([active]);
    const onSessionEvent = vi.fn();
    const props = { client, platformAdapter: platformAdapter(), sidebarStorage: storage, draftStorage: storage, onSessionEvent };
    const { rerender } = render(
      <App {...props} transcriptBySession={{ [active.session_id]: emptyTranscript() }} />,
    );
    await user.type(await screen.findByRole("textbox", { name: "Message" }), "turn A");
    await user.click(screen.getByRole("button", { name: "Send message" }));
    const submissionA = capturedSubmissionId(onSessionEvent, 0);
    let state = transcriptReducer(emptyTranscript(), event("turn_started", {
      sequence: 1,
      turn_id: "turn-a",
      submission_id: submissionA,
    }));
    rerender(<App {...props} transcriptBySession={{ [active.session_id]: state }} />);
    await act(async () => { await Promise.resolve(); });
    await user.type(screen.getByRole("textbox", { name: "Message" }), "queued B");
    await user.click(screen.getByRole("button", { name: "Queue message" }));
    state = transcriptReducer(state, event("turn_failed", { sequence: 2, turn_id: "turn-a" }));
    rerender(<App {...props} transcriptBySession={{ [active.session_id]: state }} />);

    await user.click(await screen.findByRole("button", { name: "Retry" }));
    expect(onSessionEvent).toHaveBeenLastCalledWith(active.session_id, expect.objectContaining({
      type: "send_message",
      text: "turn A",
      mode: "base",
    }));
  });

  it("binds queued B when its authoritative start wins an accepted clear race", async () => {
    const user = userEvent.setup();
    const active = session();
    const client = clientWithSessions([active]);
    const onSessionEvent = vi.fn();
    const props = { client, platformAdapter: platformAdapter(), sidebarStorage: storage, draftStorage: storage, onSessionEvent };
    const { rerender } = render(<App {...props} transcriptBySession={{ [active.session_id]: emptyTranscript() }} />);
    await user.type(await screen.findByRole("textbox", { name: "Message" }), "turn A");
    await user.click(screen.getByRole("button", { name: "Send message" }));
    const submissionA = capturedSubmissionId(onSessionEvent, 0);
    let state = transcriptReducer(emptyTranscript(), event("turn_started", {
      sequence: 1,
      turn_id: "turn-a",
      submission_id: submissionA,
    }));
    rerender(<App {...props} transcriptBySession={{ [active.session_id]: state }} />);
    await act(async () => { await Promise.resolve(); });
    await user.type(await screen.findByRole("textbox", { name: "Message" }), "retired B");
    await user.click(screen.getByRole("button", { name: "Queue message" }));
    const submissionB = capturedSubmissionId(onSessionEvent, 1);
    state = {
      ...state,
      queued: { type: "queue_message", text: "retired B", mode: "base", submission_id: submissionB },
    };
    rerender(<App {...props} transcriptBySession={{ [active.session_id]: state }} />);
    await user.click(screen.getByRole("button", { name: "Edit queued follow-up" }));
    state = transcriptReducer(state, event("turn_completed", { sequence: 2, turn_id: "turn-a" }));
    state = transcriptReducer(state, event("turn_started", {
      sequence: 3,
      turn_id: "turn-b",
      submission_id: submissionB,
    }));
    rerender(<App {...props} transcriptBySession={{ [active.session_id]: state }} />);
    await act(async () => { await Promise.resolve(); });
    state = transcriptReducer(state, event("turn_failed", { sequence: 4, turn_id: "turn-b" }));
    rerender(<App {...props} transcriptBySession={{ [active.session_id]: state }} />);

    await user.dblClick(await screen.findByRole("button", { name: "Retry" }));
    expect(onSessionEvent).toHaveBeenLastCalledWith(active.session_id, expect.objectContaining({
      type: "send_message",
      text: "retired B",
      mode: "base",
    }));
    expect(onSessionEvent).toHaveBeenCalledTimes(4);
  });

  it("restores queued B when clear rejects before B starts", async () => {
    const user = userEvent.setup();
    const active = session();
    const client = clientWithSessions([active]);
    const onSessionEvent = vi.fn((_sessionId: string, outbound: ClientEvent) => (
      outbound.type === "clear_queued_message"
        ? Promise.reject(new Error("unsafe clear detail"))
        : undefined
    ));
    const props = { client, platformAdapter: platformAdapter(), sidebarStorage: storage, draftStorage: storage, onSessionEvent };
    const { rerender } = render(<App {...props} transcriptBySession={{ [active.session_id]: emptyTranscript() }} />);
    await user.type(await screen.findByRole("textbox", { name: "Message" }), "turn A");
    await user.click(screen.getByRole("button", { name: "Send message" }));
    const submissionA = capturedSubmissionId(onSessionEvent, 0);
    let state = transcriptReducer(emptyTranscript(), event("turn_started", {
      sequence: 1,
      turn_id: "turn-a",
      submission_id: submissionA,
    }));
    rerender(<App {...props} transcriptBySession={{ [active.session_id]: state }} />);
    await act(async () => { await Promise.resolve(); });
    await user.type(screen.getByRole("textbox", { name: "Message" }), "queued B");
    await user.click(screen.getByRole("button", { name: "Queue message" }));
    const submissionB = capturedSubmissionId(onSessionEvent, 1);
    state = {
      ...state,
      queued: { type: "queue_message", text: "queued B", mode: "base", submission_id: submissionB },
    };
    rerender(<App {...props} transcriptBySession={{ [active.session_id]: state }} />);
    await user.click(screen.getByRole("button", { name: "Edit queued follow-up" }));
    state = transcriptReducer(state, event("turn_completed", { sequence: 2, turn_id: "turn-a" }));
    state = transcriptReducer(state, event("turn_started", {
      sequence: 3,
      turn_id: "turn-b",
      submission_id: submissionB,
    }));
    rerender(<App {...props} transcriptBySession={{ [active.session_id]: state }} />);
    await act(async () => { await Promise.resolve(); });
    state = transcriptReducer(state, event("turn_failed", { sequence: 4, turn_id: "turn-b" }));
    rerender(<App {...props} transcriptBySession={{ [active.session_id]: state }} />);

    await user.click(await screen.findByRole("button", { name: "Retry" }));
    expect(onSessionEvent).toHaveBeenLastCalledWith(active.session_id, expect.objectContaining({
      type: "send_message",
      text: "queued B",
      mode: "base",
    }));
    expect(screen.getByRole("alert")).not.toHaveTextContent("unsafe clear detail");
  });

  it("keeps a clearing B bound when B starts before the clear rejection settles", async () => {
    const user = userEvent.setup();
    const active = session();
    const client = clientWithSessions([active]);
    const clear = deferred();
    const onSessionEvent = vi.fn((_sessionId: string, outbound: ClientEvent) => (
      outbound.type === "clear_queued_message" ? clear.promise : undefined
    ));
    const props = { client, platformAdapter: platformAdapter(), sidebarStorage: storage, draftStorage: storage, onSessionEvent };
    const { rerender } = render(<App {...props} transcriptBySession={{ [active.session_id]: emptyTranscript() }} />);
    await user.type(await screen.findByRole("textbox", { name: "Message" }), "turn A");
    await user.click(screen.getByRole("button", { name: "Send message" }));
    const submissionA = capturedSubmissionId(onSessionEvent, 0);
    let state = transcriptReducer(emptyTranscript(), event("turn_started", {
      sequence: 1,
      turn_id: "turn-a",
      submission_id: submissionA,
    }));
    rerender(<App {...props} transcriptBySession={{ [active.session_id]: state }} />);
    await act(async () => { await Promise.resolve(); });
    await user.type(screen.getByRole("textbox", { name: "Message" }), "queued B");
    await user.click(screen.getByRole("button", { name: "Queue message" }));
    const submissionB = capturedSubmissionId(onSessionEvent, 1);
    state = {
      ...state,
      queued: { type: "queue_message", text: "queued B", mode: "base", submission_id: submissionB },
    };
    rerender(<App {...props} transcriptBySession={{ [active.session_id]: state }} />);
    await user.click(screen.getByRole("button", { name: "Edit queued follow-up" }));
    state = transcriptReducer(state, event("turn_completed", { sequence: 2, turn_id: "turn-a" }));
    state = transcriptReducer(state, event("turn_started", {
      sequence: 3,
      turn_id: "turn-b",
      submission_id: submissionB,
    }));
    rerender(<App {...props} transcriptBySession={{ [active.session_id]: state }} />);
    await act(async () => { await Promise.resolve(); });
    state = transcriptReducer(state, event("turn_failed", { sequence: 4, turn_id: "turn-b" }));
    rerender(<App {...props} transcriptBySession={{ [active.session_id]: state }} />);
    await act(async () => clear.reject(new Error("late clear rejection")));

    await user.click(await screen.findByRole("button", { name: "Retry" }));
    expect(onSessionEvent).toHaveBeenLastCalledWith(active.session_id, expect.objectContaining({
      type: "send_message",
      text: "queued B",
      mode: "base",
    }));
  });

  it("retires an accepted clearing B when a later direct C becomes authoritative", async () => {
    const user = userEvent.setup();
    const active = session();
    const client = clientWithSessions([active]);
    const onSessionEvent = vi.fn();
    const props = { client, platformAdapter: platformAdapter(), sidebarStorage: storage, draftStorage: storage, onSessionEvent };
    const { rerender } = render(<App {...props} transcriptBySession={{ [active.session_id]: emptyTranscript() }} />);
    await user.type(await screen.findByRole("textbox", { name: "Message" }), "turn A");
    await user.click(screen.getByRole("button", { name: "Send message" }));
    const submissionA = capturedSubmissionId(onSessionEvent, 0);
    let state = transcriptReducer(emptyTranscript(), event("turn_started", {
      sequence: 1,
      turn_id: "turn-a",
      submission_id: submissionA,
    }));
    rerender(<App {...props} transcriptBySession={{ [active.session_id]: state }} />);
    await act(async () => { await Promise.resolve(); });
    await user.type(screen.getByRole("textbox", { name: "Message" }), "cleared B");
    await user.click(screen.getByRole("button", { name: "Queue message" }));
    const submissionB = capturedSubmissionId(onSessionEvent, 1);
    state = {
      ...state,
      queued: { type: "queue_message", text: "cleared B", mode: "base", submission_id: submissionB },
    };
    rerender(<App {...props} transcriptBySession={{ [active.session_id]: state }} />);
    await user.click(screen.getByRole("button", { name: "Edit queued follow-up" }));

    state = transcriptReducer(state, event("turn_completed", { sequence: 2, turn_id: "turn-a" }));
    state = { ...state, queued: null };
    rerender(<App {...props} transcriptBySession={{ [active.session_id]: state }} />);
    expect(screen.queryByRole("button", { name: "Retry" })).not.toBeInTheDocument();
    const directComposer = await screen.findByRole("textbox", { name: "Message" });
    await user.clear(directComposer);
    await user.type(directComposer, "direct C");
    await user.click(screen.getByRole("button", { name: "Send message" }));
    const submissionC = capturedSubmissionId(onSessionEvent, 3);
    state = transcriptReducer(state, event("turn_started", {
      sequence: 3,
      turn_id: "turn-c",
      submission_id: submissionC,
    }));
    rerender(<App {...props} transcriptBySession={{ [active.session_id]: state }} />);
    await act(async () => { await Promise.resolve(); });
    state = transcriptReducer(state, event("turn_failed", { sequence: 4, turn_id: "turn-c" }));
    rerender(<App {...props} transcriptBySession={{ [active.session_id]: state }} />);

    await user.click(await screen.findByRole("button", { name: "Retry" }));
    expect(onSessionEvent).toHaveBeenLastCalledWith(active.session_id, expect.objectContaining({
      type: "send_message",
      text: "direct C",
      mode: "base",
    }));
    expect(onSessionEvent).toHaveBeenCalledTimes(5);
  });

  it("keeps a retried A authoritative when a late clear rejection tries to restore queued B", async () => {
    const user = userEvent.setup();
    const active = session();
    const client = clientWithSessions([active]);
    const clear = deferred();
    const onSessionEvent = vi.fn((_sessionId: string, outbound: ClientEvent) => (
      outbound.type === "clear_queued_message" ? clear.promise : undefined
    ));
    const props = { client, platformAdapter: platformAdapter(), sidebarStorage: storage, draftStorage: storage, onSessionEvent };
    const { rerender } = render(<App {...props} transcriptBySession={{ [active.session_id]: emptyTranscript() }} />);
    await user.type(await screen.findByRole("textbox", { name: "Message" }), "turn A");
    await user.click(screen.getByRole("button", { name: "Send message" }));
    const submissionA = capturedSubmissionId(onSessionEvent, 0);
    let state = transcriptReducer(emptyTranscript(), event("turn_started", {
      sequence: 1,
      turn_id: "turn-a",
      submission_id: submissionA,
    }));
    rerender(<App {...props} transcriptBySession={{ [active.session_id]: state }} />);
    await act(async () => { await Promise.resolve(); });
    await user.type(screen.getByRole("textbox", { name: "Message" }), "queued B");
    await user.click(screen.getByRole("button", { name: "Queue message" }));
    const submissionB = capturedSubmissionId(onSessionEvent, 1);
    state = {
      ...state,
      queued: { type: "queue_message", text: "queued B", mode: "base", submission_id: submissionB },
    };
    rerender(<App {...props} transcriptBySession={{ [active.session_id]: state }} />);
    await user.click(screen.getByRole("button", { name: "Edit queued follow-up" }));
    state = transcriptReducer(state, event("turn_failed", { sequence: 2, turn_id: "turn-a" }));
    rerender(<App {...props} transcriptBySession={{ [active.session_id]: state }} />);

    await user.click(await screen.findByRole("button", { name: "Retry" }));
    const retrySubmission = capturedSubmissionId(onSessionEvent, 3);
    await act(async () => clear.reject(new Error("late rejected clear")));
    state = transcriptReducer(state, event("turn_started", {
      sequence: 3,
      turn_id: "turn-a-retry",
      submission_id: retrySubmission,
    }));
    rerender(<App {...props} transcriptBySession={{ [active.session_id]: state }} />);
    await act(async () => { await Promise.resolve(); });
    state = transcriptReducer(state, event("turn_failed", { sequence: 4, turn_id: "turn-a-retry" }));
    rerender(<App {...props} transcriptBySession={{ [active.session_id]: state }} />);

    await user.click(await screen.findByRole("button", { name: "Retry" }));
    expect(onSessionEvent).toHaveBeenLastCalledWith(active.session_id, expect.objectContaining({
      type: "send_message",
      text: "turn A",
      mode: "base",
    }));
    expect(onSessionEvent).toHaveBeenCalledTimes(5);
  });

  it("binds B by exact submission id when B beats a clear and a later direct C", async () => {
    const user = userEvent.setup();
    const active = session();
    const client = clientWithSessions([active]);
    const clear = deferred();
    const onSessionEvent = vi.fn((_sessionId: string, outbound: ClientEvent) => (
      outbound.type === "clear_queued_message" ? clear.promise : undefined
    ));
    const props = { client, platformAdapter: platformAdapter(), sidebarStorage: storage, draftStorage: storage, onSessionEvent };
    const { rerender } = render(<App {...props} transcriptBySession={{ [active.session_id]: emptyTranscript() }} />);
    await user.type(await screen.findByRole("textbox", { name: "Message" }), "turn A");
    await user.click(screen.getByRole("button", { name: "Send message" }));
    const submissionA = capturedSubmissionId(onSessionEvent, 0);
    let state = transcriptReducer(emptyTranscript(), event("turn_started", {
      sequence: 1,
      turn_id: "turn-a",
      submission_id: submissionA,
    }));
    rerender(<App {...props} transcriptBySession={{ [active.session_id]: state }} />);
    await act(async () => { await Promise.resolve(); });
    await user.type(screen.getByRole("textbox", { name: "Message" }), "queued B");
    await user.click(screen.getByRole("button", { name: "Queue message" }));
    const submissionB = capturedSubmissionId(onSessionEvent, 1);
    state = {
      ...state,
      queued: {
        type: "queue_message",
        text: "queued B",
        mode: "base",
        submission_id: submissionB,
      },
    };
    rerender(<App {...props} transcriptBySession={{ [active.session_id]: state }} />);
    await user.click(screen.getByRole("button", { name: "Edit queued follow-up" }));
    state = transcriptReducer(state, event("turn_completed", { sequence: 2, turn_id: "turn-a" }));
    state = { ...state, queued: null };
    rerender(<App {...props} transcriptBySession={{ [active.session_id]: state }} />);
    const composer = await screen.findByRole("textbox", { name: "Message" });
    await user.clear(composer);
    await user.type(composer, "direct C");
    await user.click(screen.getByRole("button", { name: "Send message" }));
    capturedSubmissionId(onSessionEvent, 3);

    state = transcriptReducer(state, event("turn_started", {
      sequence: 3,
      turn_id: "turn-b",
      submission_id: submissionB,
    }));
    rerender(<App {...props} transcriptBySession={{ [active.session_id]: state }} />);
    await act(async () => { await Promise.resolve(); });
    state = transcriptReducer(state, event("turn_failed", { sequence: 4, turn_id: "turn-b" }));
    rerender(<App {...props} transcriptBySession={{ [active.session_id]: state }} />);
    await act(async () => clear.reject(new Error("late clear rejection")));

    await user.click(await screen.findByRole("button", { name: "Retry" }));
    expect(onSessionEvent).toHaveBeenLastCalledWith(active.session_id, expect.objectContaining({
      type: "send_message",
      text: "queued B",
      mode: "base",
    }));
  });

  it("never binds edited D to a stale B start with B's submission id", async () => {
    const user = userEvent.setup();
    const active = session();
    const client = clientWithSessions([active]);
    const onSessionEvent = vi.fn();
    const props = { client, platformAdapter: platformAdapter(), sidebarStorage: storage, draftStorage: storage, onSessionEvent };
    const { rerender } = render(<App {...props} transcriptBySession={{ [active.session_id]: emptyTranscript() }} />);
    await user.type(await screen.findByRole("textbox", { name: "Message" }), "turn A");
    await user.click(screen.getByRole("button", { name: "Send message" }));
    const submissionA = capturedSubmissionId(onSessionEvent, 0);
    let state = transcriptReducer(emptyTranscript(), event("turn_started", {
      sequence: 1,
      turn_id: "turn-a",
      submission_id: submissionA,
    }));
    rerender(<App {...props} transcriptBySession={{ [active.session_id]: state }} />);
    await act(async () => { await Promise.resolve(); });
    await user.type(screen.getByRole("textbox", { name: "Message" }), "queued B");
    await user.click(screen.getByRole("button", { name: "Queue message" }));
    const submissionB = capturedSubmissionId(onSessionEvent, 1);
    state = {
      ...state,
      queued: {
        type: "queue_message",
        text: "queued B",
        mode: "base",
        submission_id: submissionB,
      },
    };
    rerender(<App {...props} transcriptBySession={{ [active.session_id]: state }} />);
    await user.clear(screen.getByRole("textbox", { name: "Message" }));
    await user.type(screen.getByRole("textbox", { name: "Message" }), "edited D");
    await user.click(screen.getByRole("button", { name: "Update queued message" }));
    capturedSubmissionId(onSessionEvent, 2);

    state = transcriptReducer(state, event("turn_completed", { sequence: 2, turn_id: "turn-a" }));
    state = transcriptReducer(state, event("turn_started", {
      sequence: 3,
      turn_id: "turn-b",
      submission_id: submissionB,
    }));
    rerender(<App {...props} transcriptBySession={{ [active.session_id]: state }} />);
    await act(async () => { await Promise.resolve(); });
    state = transcriptReducer(state, event("turn_failed", { sequence: 4, turn_id: "turn-b" }));
    rerender(<App {...props} transcriptBySession={{ [active.session_id]: state }} />);

    await user.click(await screen.findByRole("button", { name: "Retry" }));
    expect(onSessionEvent).toHaveBeenLastCalledWith(active.session_id, expect.objectContaining({
      type: "send_message",
      text: "queued B",
      mode: "base",
    }));
  });

  it.each([
    ["missing", undefined],
    ["unknown", "submission-never-dispatched"],
  ])("never guesses retry authority for a %s turn-start submission id", async (_case, submissionId) => {
    const user = userEvent.setup();
    const active = session();
    const client = clientWithSessions([active]);
    const onSessionEvent = vi.fn();
    const props = { client, platformAdapter: platformAdapter(), sidebarStorage: storage, draftStorage: storage, onSessionEvent };
    const { rerender } = render(<App {...props} transcriptBySession={{ [active.session_id]: emptyTranscript() }} />);
    await user.type(await screen.findByRole("textbox", { name: "Message" }), "must stay unbound");
    await user.click(screen.getByRole("button", { name: "Send message" }));
    capturedSubmissionId(onSessionEvent, 0);
    const startedEvent = {
      ...event("turn_started", { sequence: 1, turn_id: "uncorrelated-turn" }),
      ...(submissionId === undefined ? {} : { submission_id: submissionId }),
    };
    let state = transcriptReducer(emptyTranscript(), startedEvent as never);
    rerender(<App {...props} transcriptBySession={{ [active.session_id]: state }} />);
    await act(async () => { await Promise.resolve(); });
    state = transcriptReducer(state, event("turn_failed", { sequence: 2, turn_id: "uncorrelated-turn" }));
    rerender(<App {...props} transcriptBySession={{ [active.session_id]: state }} />);

    expect(await screen.findByRole("alert")).toBeVisible();
    expect(screen.queryByRole("button", { name: "Retry" })).not.toBeInTheDocument();
  });

  it("retires only a rejected direct attempt so its late start cannot gain retry authority", async () => {
    const user = userEvent.setup();
    const active = session();
    const client = clientWithSessions([active]);
    const onSessionEvent = vi.fn(() => Promise.reject(new Error("unsafe send detail")));
    const props = { client, platformAdapter: platformAdapter(), sidebarStorage: storage, draftStorage: storage, onSessionEvent };
    const { rerender } = render(<App {...props} transcriptBySession={{ [active.session_id]: emptyTranscript() }} />);
    await user.type(await screen.findByRole("textbox", { name: "Message" }), "rejected direct");
    await user.click(screen.getByRole("button", { name: "Send message" }));
    const rejectedSubmission = capturedSubmissionId(onSessionEvent, 0);
    expect(await screen.findByRole("status", { name: "Message status" })).toHaveTextContent(
      "Message not sent",
    );

    let state = transcriptReducer(emptyTranscript(), event("turn_started", {
      sequence: 1,
      turn_id: "late-rejected-turn",
      submission_id: rejectedSubmission,
    }));
    rerender(<App {...props} transcriptBySession={{ [active.session_id]: state }} />);
    await act(async () => { await Promise.resolve(); });
    state = transcriptReducer(state, event("turn_failed", {
      sequence: 2,
      turn_id: "late-rejected-turn",
    }));
    rerender(<App {...props} transcriptBySession={{ [active.session_id]: state }} />);

    expect(await screen.findByRole("alert")).not.toHaveTextContent("unsafe send detail");
    expect(screen.queryByRole("button", { name: "Retry" })).not.toBeInTheDocument();
  });

  it("replaces a queued retry candidate when the follow-up is edited", async () => {
    const user = userEvent.setup();
    const active = session();
    const client = clientWithSessions([active]);
    const onSessionEvent = vi.fn();
    const props = { client, platformAdapter: platformAdapter(), sidebarStorage: storage, draftStorage: storage, onSessionEvent };
    const { rerender } = render(<App {...props} transcriptBySession={{ [active.session_id]: emptyTranscript() }} />);
    await user.type(await screen.findByRole("textbox", { name: "Message" }), "turn A");
    await user.click(screen.getByRole("button", { name: "Send message" }));
    const submissionA = capturedSubmissionId(onSessionEvent, 0);
    let state = transcriptReducer(emptyTranscript(), event("turn_started", {
      sequence: 1,
      turn_id: "turn-a",
      submission_id: submissionA,
    }));
    rerender(<App {...props} transcriptBySession={{ [active.session_id]: state }} />);
    await act(async () => { await Promise.resolve(); });
    await user.type(screen.getByRole("textbox", { name: "Message" }), "queued B");
    await user.click(screen.getByRole("button", { name: "Queue message" }));
    const submissionB = capturedSubmissionId(onSessionEvent, 1);
    state = {
      ...state,
      queued: { type: "queue_message", text: "queued B", mode: "base", submission_id: submissionB },
    };
    rerender(<App {...props} transcriptBySession={{ [active.session_id]: state }} />);
    await user.clear(screen.getByRole("textbox", { name: "Message" }));
    await user.type(screen.getByRole("textbox", { name: "Message" }), "edited C");
    await user.click(screen.getByRole("button", { name: "Update queued message" }));
    const submissionC = capturedSubmissionId(onSessionEvent, 2);

    state = transcriptReducer(state, event("turn_completed", { sequence: 2, turn_id: "turn-a" }));
    state = transcriptReducer(state, event("turn_started", {
      sequence: 3,
      turn_id: "turn-c",
      submission_id: submissionC,
    }));
    rerender(<App {...props} transcriptBySession={{ [active.session_id]: state }} />);
    await act(async () => { await Promise.resolve(); });
    state = transcriptReducer(state, event("turn_failed", { sequence: 4, turn_id: "turn-c" }));
    rerender(<App {...props} transcriptBySession={{ [active.session_id]: state }} />);

    await user.click(await screen.findByRole("button", { name: "Retry" }));
    expect(onSessionEvent).toHaveBeenLastCalledWith(active.session_id, expect.objectContaining({
      type: "send_message",
      text: "edited C",
      mode: "base",
    }));
    expect(onSessionEvent).not.toHaveBeenLastCalledWith(active.session_id, expect.objectContaining({
      type: "send_message",
      text: "queued B",
      mode: "base",
    }));
  });

  it("restores the prior queued retry candidate when an edit is rejected", async () => {
    const user = userEvent.setup();
    const active = session();
    const client = clientWithSessions([active]);
    const onSessionEvent = vi.fn((_sessionId: string, outbound: ClientEvent) => {
      if (outbound.type === "queue_message" && outbound.text === "edited C") {
        return Promise.reject(new Error("unsafe queue update detail"));
      }
    });
    const props = { client, platformAdapter: platformAdapter(), sidebarStorage: storage, draftStorage: storage, onSessionEvent };
    const { rerender } = render(<App {...props} transcriptBySession={{ [active.session_id]: emptyTranscript() }} />);
    await user.type(await screen.findByRole("textbox", { name: "Message" }), "turn A");
    await user.click(screen.getByRole("button", { name: "Send message" }));
    const submissionA = capturedSubmissionId(onSessionEvent, 0);
    let state = transcriptReducer(emptyTranscript(), event("turn_started", {
      sequence: 1,
      turn_id: "turn-a",
      submission_id: submissionA,
    }));
    rerender(<App {...props} transcriptBySession={{ [active.session_id]: state }} />);
    await act(async () => { await Promise.resolve(); });
    await user.type(screen.getByRole("textbox", { name: "Message" }), "queued B");
    await user.click(screen.getByRole("button", { name: "Queue message" }));
    const submissionB = capturedSubmissionId(onSessionEvent, 1);
    state = {
      ...state,
      queued: { type: "queue_message", text: "queued B", mode: "base", submission_id: submissionB },
    };
    rerender(<App {...props} transcriptBySession={{ [active.session_id]: state }} />);
    await user.clear(screen.getByRole("textbox", { name: "Message" }));
    await user.type(screen.getByRole("textbox", { name: "Message" }), "edited C");
    await user.click(screen.getByRole("button", { name: "Update queued message" }));

    state = transcriptReducer(state, event("turn_completed", { sequence: 2, turn_id: "turn-a" }));
    state = transcriptReducer(state, event("turn_started", {
      sequence: 3,
      turn_id: "turn-b",
      submission_id: submissionB,
    }));
    rerender(<App {...props} transcriptBySession={{ [active.session_id]: state }} />);
    await act(async () => { await Promise.resolve(); });
    state = transcriptReducer(state, event("turn_failed", { sequence: 4, turn_id: "turn-b" }));
    rerender(<App {...props} transcriptBySession={{ [active.session_id]: state }} />);

    await user.click(await screen.findByRole("button", { name: "Retry" }));
    expect(onSessionEvent).toHaveBeenLastCalledWith(active.session_id, expect.objectContaining({
      type: "send_message",
      text: "queued B",
      mode: "base",
    }));
    expect(screen.getByRole("alert")).not.toHaveTextContent("unsafe queue update detail");
  });

  it("keeps a rejected exact retry available without exposing its raw error", async () => {
    const user = userEvent.setup();
    const active = session();
    const client = clientWithSessions([active]);
    let sends = 0;
    const onSessionEvent = vi.fn((_sessionId: string, outbound: ClientEvent) => {
      if (outbound.type !== "send_message") return;
      sends += 1;
      if (sends === 2) return Promise.reject(new Error("Bearer secret-retry-detail"));
    });
    const props = { client, platformAdapter: platformAdapter(), sidebarStorage: storage, draftStorage: storage, onSessionEvent };
    const { rerender } = render(<App {...props} transcriptBySession={{ [active.session_id]: emptyTranscript() }} />);
    await user.type(await screen.findByRole("textbox", { name: "Message" }), "retry me");
    await user.click(screen.getByRole("button", { name: "Send message" }));
    const submissionA = capturedSubmissionId(onSessionEvent, 0);
    let state = transcriptReducer(emptyTranscript(), event("turn_started", {
      sequence: 1,
      turn_id: "turn-a",
      submission_id: submissionA,
    }));
    rerender(<App {...props} transcriptBySession={{ [active.session_id]: state }} />);
    await act(async () => { await Promise.resolve(); });
    state = transcriptReducer(state, event("turn_failed", { sequence: 2, turn_id: "turn-a" }));
    rerender(<App {...props} transcriptBySession={{ [active.session_id]: state }} />);

    await user.click(await screen.findByRole("button", { name: "Retry" }));
    expect(await screen.findByRole("status", { name: "Recovery action failed" }))
      .toHaveTextContent("The recovery action failed.");
    expect(screen.getByRole("alert")).not.toHaveTextContent("secret-retry-detail");
    await user.click(screen.getByRole("button", { name: "Retry" }));
    expect(sends).toBe(3);
    expect(onSessionEvent).toHaveBeenLastCalledWith(active.session_id, expect.objectContaining({
      type: "send_message",
      text: "retry me",
      mode: "base",
    }));
  });

  it("does not offer a retained turn retry after the client or generation authority is replaced", async () => {
    const user = userEvent.setup();
    const active = session();
    const firstClient = clientWithSessions([active]);
    const onSessionEvent = vi.fn();
    const props = {
      platformAdapter: platformAdapter(),
      sidebarStorage: storage,
      draftStorage: storage,
      onSessionEvent,
    };
    const { rerender } = render(
      <App
        {...props}
        client={firstClient}
        transcriptBySession={{ [active.session_id]: emptyTranscript() }}
      />,
    );
    await user.type(await screen.findByRole("textbox", { name: "Message" }), "stale submission");
    await user.click(screen.getByRole("button", { name: "Send message" }));
    const submission = capturedSubmissionId(onSessionEvent, 0);

    const generationOne = transcriptReducer(
      emptyTranscript(),
      event("turn_started", {
        generation: 1,
        sequence: 1,
        turn_id: "generation-one",
        submission_id: submission,
      }),
    );
    rerender(
      <App
        {...props}
        client={firstClient}
        transcriptBySession={{ [active.session_id]: generationOne }}
      />,
    );
    await act(async () => { await Promise.resolve(); });

    let generationTwo = transcriptReducer(
      emptyTranscript(),
      event("session_snapshot", { generation: 2, sequence: 1 }),
    );
    generationTwo = transcriptReducer(
      generationTwo,
      event("turn_failed", { generation: 2, sequence: 2 }),
    );
    rerender(
      <App
        {...props}
        client={firstClient}
        transcriptBySession={{ [active.session_id]: generationTwo }}
      />,
    );
    expect(await screen.findByRole("alert")).toBeVisible();
    expect(screen.queryByRole("button", { name: "Retry" })).not.toBeInTheDocument();

    rerender(
      <App
        {...props}
        client={clientWithSessions([active])}
        transcriptBySession={{ [active.session_id]: generationTwo }}
      />,
    );
    expect(screen.queryByRole("button", { name: "Retry" })).not.toBeInTheDocument();
  });

  it("removes retry authority when the dispatcher changes", async () => {
    const user = userEvent.setup();
    const active = session();
    const client = clientWithSessions([active]);
    const firstDispatcher = vi.fn();
    const secondDispatcher = vi.fn();
    const baseProps = {
      client,
      platformAdapter: platformAdapter(),
      sidebarStorage: storage,
      draftStorage: storage,
    };
    const { rerender } = render(
      <App
        {...baseProps}
        onSessionEvent={firstDispatcher}
        transcriptBySession={{ [active.session_id]: emptyTranscript() }}
      />,
    );
    await user.type(await screen.findByRole("textbox", { name: "Message" }), "dispatcher-bound");
    await user.click(screen.getByRole("button", { name: "Send message" }));
    const submission = capturedSubmissionId(firstDispatcher, 0);
    const started = transcriptReducer(
      emptyTranscript(),
      event("turn_started", { sequence: 1, turn_id: "turn-a", submission_id: submission }),
    );
    rerender(
      <App
        {...baseProps}
        onSessionEvent={firstDispatcher}
        transcriptBySession={{ [active.session_id]: started }}
      />,
    );
    await act(async () => { await Promise.resolve(); });
    const failed = transcriptReducer(
      started,
      event("turn_failed", { sequence: 2, turn_id: "turn-a" }),
    );
    rerender(
      <App
        {...baseProps}
        onSessionEvent={secondDispatcher}
        transcriptBySession={{ [active.session_id]: failed }}
      />,
    );

    expect(await screen.findByRole("alert")).toBeVisible();
    expect(screen.queryByRole("button", { name: "Retry" })).not.toBeInTheDocument();
  });

  it("invalidates an accepted retry candidate when its client and dispatcher are replaced", async () => {
    const user = userEvent.setup();
    const active = session();
    const firstClient = clientWithSessions([active]);
    const firstDispatcher = vi.fn();
    const secondDispatcher = vi.fn();
    const baseProps = {
      platformAdapter: platformAdapter(),
      sidebarStorage: storage,
      draftStorage: storage,
    };
    const { rerender } = render(
      <App
        {...baseProps}
        client={firstClient}
        onSessionEvent={firstDispatcher}
        transcriptBySession={{ [active.session_id]: emptyTranscript() }}
      />,
    );
    await user.type(await screen.findByRole("textbox", { name: "Message" }), "accepted then replaced");
    await user.click(screen.getByRole("button", { name: "Send message" }));
    const submission = capturedSubmissionId(firstDispatcher, 0);
    let state = transcriptReducer(
      emptyTranscript(),
      event("turn_started", { sequence: 1, turn_id: "turn-a", submission_id: submission }),
    );
    rerender(
      <App {...baseProps} client={firstClient} onSessionEvent={firstDispatcher} transcriptBySession={{ [active.session_id]: state }} />,
    );
    await act(async () => { await Promise.resolve(); });
    state = transcriptReducer(state, event("turn_failed", { sequence: 2, turn_id: "turn-a" }));
    rerender(
      <App {...baseProps} client={firstClient} onSessionEvent={firstDispatcher} transcriptBySession={{ [active.session_id]: state }} />,
    );
    await user.click(await screen.findByRole("button", { name: "Retry" }));
    expect(firstDispatcher).toHaveBeenCalledTimes(2);
    const retrySubmission = capturedSubmissionId(firstDispatcher, 1);

    state = transcriptReducer(state, event("turn_started", {
      sequence: 3,
      turn_id: "turn-b",
      submission_id: retrySubmission,
    }));
    rerender(
      <App
        {...baseProps}
        client={clientWithSessions([active])}
        onSessionEvent={secondDispatcher}
        transcriptBySession={{ [active.session_id]: state }}
      />,
    );
    await act(async () => { await Promise.resolve(); });
    state = transcriptReducer(state, event("turn_failed", { sequence: 4, turn_id: "turn-b" }));
    rerender(
      <App
        {...baseProps}
        client={clientWithSessions([active])}
        onSessionEvent={secondDispatcher}
        transcriptBySession={{ [active.session_id]: state }}
      />,
    );

    expect(await screen.findByRole("alert")).toBeVisible();
    expect(screen.queryByRole("button", { name: "Retry" })).not.toBeInTheDocument();
    expect(secondDispatcher).not.toHaveBeenCalled();
  });

  it("does not replay a submission bound to a different failed turn id", async () => {
    const user = userEvent.setup();
    const active = session();
    const client = clientWithSessions([active]);
    const onSessionEvent = vi.fn();
    const props = { client, platformAdapter: platformAdapter(), sidebarStorage: storage, draftStorage: storage, onSessionEvent };
    const { rerender } = render(
      <App {...props} transcriptBySession={{ [active.session_id]: emptyTranscript() }} />,
    );
    await user.type(await screen.findByRole("textbox", { name: "Message" }), "turn-bound");
    await user.click(screen.getByRole("button", { name: "Send message" }));
    const submission = capturedSubmissionId(onSessionEvent, 0);
    const started = transcriptReducer(
      emptyTranscript(),
      event("turn_started", { sequence: 1, turn_id: "turn-a", submission_id: submission }),
    );
    rerender(<App {...props} transcriptBySession={{ [active.session_id]: started }} />);
    await act(async () => { await Promise.resolve(); });
    const failedOtherTurn = transcriptReducer(
      started,
      event("turn_failed", { sequence: 2, turn_id: "turn-b" }),
    );
    rerender(<App {...props} transcriptBySession={{ [active.session_id]: failedOtherTurn }} />);

    expect(await screen.findByRole("alert")).toBeVisible();
    expect(screen.queryByRole("button", { name: "Retry" })).not.toBeInTheDocument();
  });

  it("checks authenticated health before exposing a platform client as connected", async () => {
    const paths: string[] = [];
    const fetchRequest: typeof fetch = async (input) => {
      const path = new URL(input instanceof Request ? input.url : input.toString()).pathname;
      paths.push(path);
      if (path === "/api/health") {
        return Response.json({ status: "ok", service_id: testServiceId });
      }
      if (path === "/api/config") return Response.json({
        base_workspace: "/work/base",
        default_mode: "default",
        default_context_mode: "compaction",
        modes: ["default", "acceptAll", "readOnly"],
        context_modes: ["compaction", "folding"],
      });
      if (path === "/api/sessions") return Response.json([session()]);
      return new Response(null, { status: 404 });
    };
    vi.stubGlobal("fetch", fetchRequest);
    render(
      <App
        platformAdapter={platformAdapter()}
        sidebarStorage={storage}
        draftStorage={storage}
      />,
    );
    expect(await screen.findByRole("heading", { name: "project-a" })).toBeVisible();
    expect(paths[0]).toBe("/api/health");
    expect(screen.getByRole("status", { name: "Local service connected" })).toBeVisible();
  });

  it("does not become create-ready when bootstrap health succeeds but session identity health fails", async () => {
    vi.useFakeTimers();
    const serviceId = "11111111-1111-4111-8111-111111111111";
    let healthRequests = 0;
    let creates = 0;
    const identityStorage = {
      getItem: (key: string) => key.includes(serviceId)
        ? JSON.stringify({ version: 1, sessionIds: ["stale"] })
        : null,
      setItem: () => {},
    };
    const fetchRequest: typeof fetch = async (input, init) => {
      const path = new URL(input instanceof Request ? input.url : input.toString()).pathname;
      if (path === "/api/health") {
        healthRequests += 1;
        return healthRequests === 2
          ? new Response(null, { status: 503 })
          : Response.json({ status: "ok", service_id: serviceId });
      }
      if (path === "/api/config") return Response.json({
        base_workspace: "/work/project-a",
        default_mode: "default",
        default_context_mode: "compaction",
        modes: ["default", "acceptAll", "readOnly"],
        context_modes: ["compaction", "folding"],
      });
      if (path === "/api/sessions" && init?.method === "GET") {
        return Response.json([
          session({ session_id: "stale", title: "Stale" }),
          session({ session_id: "current", title: "Current" }),
        ]);
      }
      if (path === "/api/sessions" && init?.method === "POST") {
        creates += 1;
        return Response.json(session({ session_id: "created" }), { status: 201 });
      }
      return new Response(null, { status: 404 });
    };
    vi.stubGlobal("fetch", fetchRequest);
    render(
      <App
        platformAdapter={platformAdapter()}
        sidebarStorage={identityStorage}
        draftStorage={storage}
      />,
    );

    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
      for (let index = 0; index < 12; index += 1) await Promise.resolve();
    });
    expect(healthRequests).toBe(2);
    expect(screen.getByRole("status", { name: "Local service reconnecting" })).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "New chat" }));
    await act(async () => { await Promise.resolve(); });
    expect(creates).toBe(0);

    await act(async () => { await vi.advanceTimersByTimeAsync(10_000); });
    fireEvent.click(screen.getByRole("button", { name: "Retry connection" }));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
      for (let index = 0; index < 12; index += 1) await Promise.resolve();
    });
    expect(screen.getByRole("button", { name: /^Current,/ })).toBeVisible();
    expect(screen.queryByRole("button", { name: /^Stale,/ })).not.toBeInTheDocument();
    expect(screen.getByRole("status", { name: "Local service connected" })).toBeVisible();
  });

  it("derives zero-session onboarding checking and ready states from current session readiness", async () => {
    const sessions = deferred<Response>();
    const config = deferred<Response>();
    const fetchRequest: typeof fetch = async (input) => {
      const path = new URL(input instanceof Request ? input.url : input.toString()).pathname;
      return path === "/api/sessions" ? sessions.promise : config.promise;
    };
    render(
      <App
        client={clientWith(fetchRequest)}
        platformAdapter={platformAdapter()}
        sidebarStorage={storage}
        draftStorage={storage}
      />,
    );
    expect(screen.getByRole("status", { name: "Checking local service" })).toBeVisible();
    expect(screen.queryByRole("textbox", { name: "Workspace path" })).not.toBeInTheDocument();

    sessions.resolve(Response.json([]));
    config.resolve(Response.json({
      base_workspace: "/work/base",
      default_mode: "default",
      default_context_mode: "compaction",
      modes: ["default", "acceptAll", "readOnly"],
      context_modes: ["compaction", "folding"],
    }));
    expect(await screen.findByRole("textbox", { name: "Workspace path" })).toHaveValue("/work/base");
  });

  it("escalates a current refresh failure after ten seconds and a successful retry clears it", async () => {
    vi.useFakeTimers();
    let lists = 0;
    const fetchRequest: typeof fetch = async (input) => {
      const path = new URL(input instanceof Request ? input.url : input.toString()).pathname;
      if (path === "/api/config") return Response.json({
        base_workspace: "/work/base",
        default_mode: "default",
        default_context_mode: "compaction",
        modes: ["default", "acceptAll", "readOnly"],
        context_modes: ["compaction", "folding"],
      });
      if (path === "/api/sessions") {
        lists += 1;
        return lists === 1
          ? Response.json({ error: { type: "request_failed", message: "Service unavailable" } }, { status: 503 })
          : Response.json([session()]);
      }
      return new Response(null, { status: 404 });
    };
    render(
      <App
        client={clientWith(fetchRequest)}
        platformAdapter={platformAdapter()}
        sidebarStorage={storage}
        draftStorage={storage}
      />,
    );
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    expect(screen.getByRole("status", { name: "Local service reconnecting" })).toBeVisible();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    await act(async () => { await vi.advanceTimersByTimeAsync(10_000); });
    expect(screen.getByRole("alert")).toHaveTextContent("Still reconnecting to the local service");
    fireEvent.click(screen.getByRole("button", { name: "Retry connection" }));
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    expect(screen.getByRole("status", { name: "Local service connected" })).toBeVisible();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    vi.useRealTimers();
  });

  it("does not let a stale failed refresh flip a replacement client into reconnecting", async () => {
    const oldSessions = deferred<Response>();
    const oldConfig = deferred<Response>();
    const oldClient = clientWith(async (input) => {
      const path = new URL(input instanceof Request ? input.url : input.toString()).pathname;
      return path === "/api/sessions" ? oldSessions.promise : oldConfig.promise;
    });
    const replacement = clientWithSessions([session()]);
    const props = {
      platformAdapter: platformAdapter(),
      sidebarStorage: storage,
      draftStorage: storage,
    };
    const { rerender } = render(<App {...props} client={oldClient} />);
    rerender(<App {...props} client={replacement} />);
    expect(await screen.findByRole("heading", { name: "project-a" })).toBeVisible();

    oldSessions.resolve(Response.json({ error: { type: "request_failed", message: "Old failure" } }, { status: 503 }));
    oldConfig.resolve(Response.json({
      base_workspace: "/work/old",
      default_mode: "default",
      default_context_mode: "compaction",
      modes: ["default", "acceptAll", "readOnly"],
      context_modes: ["compaction", "folding"],
    }));
    await act(async () => { await Promise.resolve(); });
    expect(screen.queryByRole("status", { name: "Local service reconnecting" })).not.toBeInTheDocument();
    expect(screen.getByRole("status", { name: "Local service connected" })).toBeVisible();
  });

  it("keeps a typed New chat failure scoped and retries the exact mutation without claiming a disconnect", async () => {
    const user = userEvent.setup();
    const active = session();
    const bodies: unknown[] = [];
    const fetchRequest: typeof fetch = async (input, init) => {
      const path = new URL(input instanceof Request ? input.url : input.toString()).pathname;
      if (path === "/api/config") return Response.json({
        base_workspace: "/work/project-a",
        default_mode: "default",
        default_context_mode: "compaction",
        modes: ["default", "acceptAll", "readOnly"],
        context_modes: ["compaction", "folding"],
      });
      if (path === "/api/sessions" && init?.method === "GET") return Response.json([active]);
      if (path === "/api/sessions" && init?.method === "POST") {
        bodies.push(JSON.parse(String(init.body)));
        return bodies.length === 1
          ? Response.json({
              error: { type: "credential_prerequisite", message: "unsafe token detail" },
            }, { status: 401 })
          : Response.json(session({ session_id: "retry-created", title: "New chat" }), { status: 201 });
      }
      return new Response(null, { status: 404 });
    };
    render(
      <App
        client={clientWith(fetchRequest)}
        platformAdapter={platformAdapter()}
        sidebarStorage={storage}
        draftStorage={storage}
        transcriptBySession={{ [active.session_id]: emptyTranscript() }}
      />,
    );
    await screen.findByRole("heading", { name: "project-a" });

    await user.click(screen.getByRole("button", { name: "New chat" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Run codex login, then retry.");
    expect(screen.getByRole("alert")).not.toHaveTextContent("unsafe token detail");
    expect(screen.getByRole("status", { name: "Local service connected" })).toBeVisible();
    expect(screen.queryByRole("status", { name: "Local service reconnecting" })).not.toBeInTheDocument();

    await user.dblClick(screen.getByRole("button", { name: "Retry New chat" }));
    expect(bodies).toHaveLength(2);
    expect(bodies[1]).toEqual(bodies[0]);
    expect(await screen.findByRole("heading", { name: "project-a" })).toBeVisible();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("opens selected activity detail from the transcript and overview from the header", async () => {
    const user = userEvent.setup();
    const active = session();
    const transcript = {
      ...emptyTranscript(),
      activities: {
        "activity-app": {
          activityId: "activity-app",
          turnId: "turn-app",
          parentActivityId: null,
          actor: "tool",
          name: "read_file",
          args: { path: "README.md" },
          startedAt: "2026-08-09T04:00:00Z",
          status: "complete" as const,
          result: "complete file contents",
          isError: false,
          durationMs: 12,
        },
      },
      activityOrder: ["activity-app"],
      timeline: [{ kind: "activity" as const, activityId: "activity-app" }],
    };

    render(
      <App
        client={clientWithSessions([active])}
        sidebarStorage={storage}
        draftStorage={storage}
        transcriptBySession={{ [active.session_id]: transcript }}
      />,
    );

    await user.click(await screen.findByRole("button", { name: /Open activity: read file/i }));
    expect(screen.getByRole("dialog", { name: "Activity inspector" })).toBeVisible();
    expect(screen.getByRole("region", { name: "Selected activity detail" })).toHaveTextContent(
      "complete file contents",
    );
    await user.click(screen.getByRole("button", { name: "Close activity inspector" }));
    await user.click(screen.getByRole("button", { name: "Activity" }));
    expect(screen.getByRole("dialog", { name: "Activity inspector" })).toBeVisible();
    expect(screen.queryByRole("region", { name: "Selected activity detail" })).not.toBeInTheDocument();
  });

  it("owns one exact inspector shortcut and isolates pinned open state by active session", async () => {
    const user = userEvent.setup();
    const sessionA = session({ session_id: "inspector-a", title: "Inspector A" });
    const sessionB = session({ session_id: "inspector-b", title: "Inspector B" });
    const values = new Map<string, string>();
    const inspectorStorage = {
      getItem: (key: string) => values.get(key) ?? null,
      setItem: (key: string, value: string) => { values.set(key, value); },
    };
    render(
      <App
        client={clientWithSessions([sessionA, sessionB])}
        sidebarStorage={storage}
        draftStorage={storage}
        inspectorStorage={inspectorStorage}
        transcriptBySession={{
          [sessionA.session_id]: emptyTranscript(),
          [sessionB.session_id]: emptyTranscript(),
        }}
      />,
    );

    await screen.findByRole("heading", { name: "project-a" });
    fireEvent.keyDown(window, { key: "i", metaKey: true, shiftKey: true, repeat: true });
    fireEvent.keyDown(window, { key: "i", metaKey: true, shiftKey: true, altKey: true });
    expect(screen.queryByRole("dialog", { name: "Activity inspector" })).not.toBeInTheDocument();
    fireEvent.keyDown(window, { key: "i", metaKey: true, shiftKey: true });
    expect(screen.getAllByRole("dialog", { name: "Activity inspector" })).toHaveLength(1);
    await user.click(screen.getByRole("button", { name: "Pin inspector" }));

    await selectSession(user, sessionB.title);
    expect(screen.queryByRole("dialog", { name: "Activity inspector" })).not.toBeInTheDocument();
    await selectSession(user, sessionA.title);
    expect(screen.getByRole("dialog", { name: "Activity inspector" })).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Close activity inspector" }));
    expect(values.get("agent-harness:inspector-pinned:inspector-a")).toBe("false");
  });

  it("uses the visible palette activity command as one real toggle", async () => {
    const user = userEvent.setup();
    const active = session({ session_id: "palette-toggle" });
    render(
      <App
        client={clientWithSessions([active])}
        sidebarStorage={storage}
        draftStorage={storage}
        transcriptBySession={{ [active.session_id]: emptyTranscript() }}
      />,
    );

    await user.click(await screen.findByRole("button", { name: "Activity" }));
    expect(screen.getByRole("dialog", { name: "Activity inspector" })).toBeVisible();
    await user.keyboard("{Meta>}k{/Meta}");
    await user.click(screen.getByRole("button", { name: /Toggle activity.*⌘⇧I/ }));
    expect(screen.queryByRole("dialog", { name: "Activity inspector" })).not.toBeInTheDocument();

    await user.keyboard("{Meta>}k{/Meta}");
    await user.click(screen.getByRole("button", { name: /Toggle activity.*⌘⇧I/ }));
    expect(screen.getAllByRole("dialog", { name: "Activity inspector" })).toHaveLength(1);
    expect(screen.queryByRole("region", { name: "Selected activity detail" })).not.toBeInTheDocument();
  });

  it("keeps failed bootstrap recoverable and retries fresh acquisition plus authenticated health", async () => {
    vi.useFakeTimers();
    let acquisitions = 0;
    const paths: string[] = [];
    const fetchRequest: typeof fetch = async (input) => {
      const path = new URL(input instanceof Request ? input.url : input.toString()).pathname;
      paths.push(path);
      if (path === "/api/health") {
        return Response.json({ status: "ok", service_id: testServiceId });
      }
      if (path === "/api/config") return Response.json({
        base_workspace: "/work/project-a",
        default_mode: "default",
        default_context_mode: "compaction",
        modes: ["default", "acceptAll", "readOnly"],
        context_modes: ["compaction", "folding"],
      });
      if (path === "/api/sessions") return Response.json([session()]);
      return new Response(null, { status: 404 });
    };
    vi.stubGlobal("fetch", fetchRequest);
    render(
      <App
        platformAdapter={platformAdapter({
          getServiceConnection: async () => {
            acquisitions += 1;
            if (acquisitions === 1) throw new Error("Sidecar is still starting");
            return connection;
          },
        })}
        sidebarStorage={storage}
        draftStorage={storage}
      />,
    );

    expect(screen.getByRole("navigation", { name: "Sessions" })).toBeVisible();
    expect(screen.getByRole("main")).toBeVisible();
    expect(screen.getByRole("status", { name: "Local service connecting" })).toBeVisible();
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    expect(screen.getByRole("status", { name: "Local service reconnecting" })).toBeVisible();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    await act(async () => { await vi.advanceTimersByTimeAsync(10_000); });
    expect(screen.getByRole("alert")).toHaveTextContent("Still reconnecting to the local service");

    fireEvent.click(screen.getByRole("button", { name: "Retry connection" }));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
      for (let index = 0; index < 12; index += 1) await Promise.resolve();
    });
    expect(acquisitions).toBe(2);
    expect(paths[0]).toBe("/api/health");
    expect(screen.getByRole("heading", { name: "project-a" })).toBeVisible();
    expect(screen.getByRole("status", { name: "Local service connected" })).toBeVisible();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
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
        draftStorage={storage}
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

  it("routes typed composer events for the active session and keeps Command+F ownership singular", async () => {
    const user = userEvent.setup();
    const onSessionEvent = vi.fn();
    const fetchRequest: typeof fetch = async (input, init) => {
      const url = new URL(input instanceof Request ? input.url : input.toString());
      if (url.pathname === "/api/config") {
        return Response.json({
          base_workspace: "/work/project-a",
          default_mode: "default",
          default_context_mode: "compaction",
          modes: ["default", "acceptAll", "readOnly"],
          context_modes: ["compaction", "folding"],
        });
      }
      if (url.pathname === "/api/sessions" && init?.method === "GET") {
        return Response.json([session()]);
      }
      return new Response(null, { status: 404 });
    };
    const transcript = {
      ...emptyTranscript(),
      messages: [{ role: "assistant", content: "Searchable answer" }],
    };

    render(
      <App
        client={clientWith(fetchRequest)}
        sidebarStorage={storage}
        draftStorage={storage}
        transcriptBySession={{ "session-a": transcript }}
        onSessionEvent={onSessionEvent}
      />,
    );

    const textbox = await screen.findByRole("textbox", { name: "Message" });
    expect(screen.getByText("Searchable answer")).toBeVisible();
    await user.type(textbox, "routed message");
    await user.keyboard("{Meta>}{Enter}{/Meta}");

    expect(onSessionEvent).toHaveBeenCalledWith("session-a", expect.objectContaining({
      type: "send_message",
      text: "routed message",
      mode: "base",
    }));
    await waitFor(() => expect(textbox).toHaveValue(""));
    await user.type(textbox, "draft survives search");
    await act(async () => {
      fireEvent.keyDown(window, { key: "f", metaKey: true });
      await Promise.resolve();
    });
    expect(screen.getAllByRole("searchbox", { name: "Search conversation" })).toHaveLength(1);
    expect(textbox).toHaveValue("draft survives search");
    await user.click(screen.getByRole("button", { name: "Close conversation search" }));
    await waitFor(() =>
      expect(screen.queryByRole("searchbox", { name: "Search conversation" })).not.toBeInTheDocument(),
    );
  });

  it("routes inline decisions with the active session identity", async () => {
    const user = userEvent.setup();
    const onSessionEvent = vi.fn();
    const sessionA = session({
      session_id: "decision-a",
      title: "Decision A",
      workspace: "/work/decision-a",
    });
    const sessionB = session({
      session_id: "decision-b",
      title: "Decision B",
      workspace: "/work/decision-b",
    });
    const permissionState = transcriptReducer(
      transcriptReducer(emptyTranscript(), event("turn_started", { sequence: 1 })),
      event("permission_requested", {
        sequence: 2,
        request_id: "permission-a",
        action: "write_file",
        scope: '{"path":"A.md"}',
      }),
    );
    const planState = transcriptReducer(
      transcriptReducer(emptyTranscript(), event("turn_started", { sequence: 1 })),
      event("plan_approval_requested", {
        sequence: 2,
        request_id: "plan-b",
        plan: "1. Verify B",
      }),
    );

    render(
      <App
        client={clientWithSessions([sessionA, sessionB])}
        sidebarStorage={storage}
        draftStorage={storage}
        transcriptBySession={{
          [sessionA.session_id]: permissionState,
          [sessionB.session_id]: planState,
        }}
        onSessionEvent={onSessionEvent}
      />,
    );

    await user.click(await screen.findByRole("button", { name: /^Allow once/ }));
    expect(onSessionEvent).toHaveBeenCalledWith(sessionA.session_id, {
      type: "answer_permission",
      request_id: "permission-a",
      answer: "yes",
    });

    await selectSession(user, sessionB.title);
    await user.click(screen.getByRole("button", { name: "Approve plan" }));
    expect(onSessionEvent).toHaveBeenLastCalledWith(sessionB.session_id, {
      type: "answer_plan",
      request_id: "plan-b",
      approved: true,
    });
  });

  it("restores an authoritative queue clear only to session A after it resolves while B is active", async () => {
    const user = userEvent.setup();
    const clear = deferred();
    const sessionA = session({
      session_id: "clear-success-a",
      title: "Clear success A",
      workspace: "/work/clear-success-a",
    });
    const sessionB = session({
      session_id: "clear-success-b",
      title: "Clear success B",
      workspace: "/work/clear-success-b",
    });
    const client = clientWithSessions([sessionA, sessionB]);
    const onSessionEvent = vi.fn((sessionId: string, event: ClientEvent) =>
      sessionId === sessionA.session_id && event.type === "clear_queued_message"
        ? clear.promise
        : undefined);
    const initialTranscripts = {
      [sessionA.session_id]: {
        ...emptyTranscript(),
        running: true,
        queued: { type: "queue_message" as const, text: "restore A queue", mode: "plan" as const },
      },
      [sessionB.session_id]: emptyTranscript(),
    };
    const appProps = {
      client,
      sidebarStorage: storage,
      draftStorage: storage,
      onSessionEvent,
    };
    const { rerender } = render(<App {...appProps} transcriptBySession={initialTranscripts} />);

    await screen.findByRole("textbox", { name: "Message" });
    await user.click(screen.getByRole("button", { name: "Edit queued follow-up" }));
    await selectSession(user, sessionB.title);
    const textbox = screen.getByRole("textbox", { name: "Message" });
    await user.type(textbox, "B remains editable");
    rerender(
      <App
        {...appProps}
        transcriptBySession={{
          ...initialTranscripts,
          [sessionA.session_id]: { ...initialTranscripts[sessionA.session_id], queued: null },
        }}
      />,
    );
    await act(async () => {
      clear.resolve();
      await clear.promise;
    });

    expect(textbox).toHaveValue("B remains editable");
    expect(screen.queryByRole("status", { name: "Queue reconciliation" })).not.toBeInTheDocument();
    await selectSession(user, sessionA.title);
    expect(screen.getByRole("textbox", { name: "Message" })).toHaveValue("restore A queue");
    expect(screen.getByRole("button", { name: "Plan mode" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
  });

  it("keeps a stale queue-clear reconciliation with session A across an App switch", async () => {
    const user = userEvent.setup();
    const clear = deferred();
    const sessionA = session({
      session_id: "clear-stale-a",
      title: "Clear stale A",
      workspace: "/work/clear-stale-a",
    });
    const sessionB = session({
      session_id: "clear-stale-b",
      title: "Clear stale B",
      workspace: "/work/clear-stale-b",
    });
    const client = clientWithSessions([sessionA, sessionB]);
    const onSessionEvent = vi.fn((sessionId: string, event: ClientEvent) =>
      sessionId === sessionA.session_id && event.type === "clear_queued_message"
        ? clear.promise
        : undefined);
    const initialTranscripts = {
      [sessionA.session_id]: {
        ...emptyTranscript(),
        running: true,
        queued: { type: "queue_message" as const, text: "stale A queue", mode: "plan" as const },
      },
      [sessionB.session_id]: emptyTranscript(),
    };
    const appProps = {
      client,
      sidebarStorage: storage,
      draftStorage: storage,
      onSessionEvent,
    };
    const { rerender } = render(<App {...appProps} transcriptBySession={initialTranscripts} />);

    const textbox = await screen.findByRole("textbox", { name: "Message" });
    await user.click(screen.getByRole("button", { name: "Edit queued follow-up" }));
    await user.type(textbox, "new A draft");
    await selectSession(user, sessionB.title);
    await user.type(screen.getByRole("textbox", { name: "Message" }), "B draft");
    rerender(
      <App
        {...appProps}
        transcriptBySession={{
          ...initialTranscripts,
          [sessionA.session_id]: { ...initialTranscripts[sessionA.session_id], queued: null },
        }}
      />,
    );
    await act(async () => {
      clear.resolve();
      await clear.promise;
    });

    expect(screen.getByRole("textbox", { name: "Message" })).toHaveValue("B draft");
    expect(screen.queryByRole("status", { name: "Queue reconciliation" })).not.toBeInTheDocument();
    await selectSession(user, sessionA.title);
    expect(screen.getByRole("textbox", { name: "Message" })).toHaveValue("new A draft");
    expect(screen.getByRole("status", { name: "Queue reconciliation" })).toHaveTextContent(
      "stale A queue",
    );
  });

  it("returns a rejected queue clear and its retry only to session A", async () => {
    const user = userEvent.setup();
    const clear = deferred();
    let clearAttempts = 0;
    const sessionA = session({
      session_id: "clear-failure-a",
      title: "Clear failure A",
      workspace: "/work/clear-failure-a",
    });
    const sessionB = session({
      session_id: "clear-failure-b",
      title: "Clear failure B",
      workspace: "/work/clear-failure-b",
    });
    const onSessionEvent = vi.fn((sessionId: string, event: ClientEvent) => {
      if (sessionId !== sessionA.session_id || event.type !== "clear_queued_message") return;
      clearAttempts += 1;
      return clearAttempts === 1 ? clear.promise : undefined;
    });
    render(
      <App
        client={clientWithSessions([sessionA, sessionB])}
        sidebarStorage={storage}
        draftStorage={storage}
        transcriptBySession={{
          [sessionA.session_id]: {
            ...emptyTranscript(),
            running: true,
            queued: { type: "queue_message", text: "retry A queue", mode: "base" },
          },
          [sessionB.session_id]: emptyTranscript(),
        }}
        onSessionEvent={onSessionEvent}
      />,
    );

    await screen.findByRole("textbox", { name: "Message" });
    await user.click(screen.getByRole("button", { name: "Edit queued follow-up" }));
    await selectSession(user, sessionB.title);
    await user.type(screen.getByRole("textbox", { name: "Message" }), "B survives failure");
    await act(async () => {
      clear.reject(new Error("offline"));
      await clear.promise.catch(() => {});
    });

    expect(screen.getByRole("textbox", { name: "Message" })).toHaveValue("B survives failure");
    expect(screen.queryByRole("status", { name: "Queue reconciliation" })).not.toBeInTheDocument();
    await selectSession(user, sessionA.title);
    expect(screen.getByRole("status", { name: "Queue reconciliation" })).toHaveTextContent(
      "Queued follow-up was not cleared",
    );
    await user.click(screen.getByRole("button", { name: "Retry clearing follow-up" }));
    expect(onSessionEvent).toHaveBeenLastCalledWith(sessionA.session_id, {
      type: "clear_queued_message",
    });
    expect(clearAttempts).toBe(2);
  });

  it("clears only session A's authoritative draft when delivery succeeds while B is active", async () => {
    const user = userEvent.setup();
    const delivery = deferred();
    const sessionA = session({
      session_id: "delivery-success-a",
      title: "Delivery success A",
      workspace: "/work/delivery-success-a",
    });
    const sessionB = session({
      session_id: "delivery-success-b",
      title: "Delivery success B",
      workspace: "/work/delivery-success-b",
    });
    const onSessionEvent = vi.fn((sessionId: string) =>
      sessionId === sessionA.session_id ? delivery.promise : undefined);
    render(
      <App
        client={clientWithSessions([sessionA, sessionB])}
        sidebarStorage={storage}
        draftStorage={storage}
        transcriptBySession={{
          [sessionA.session_id]: emptyTranscript(),
          [sessionB.session_id]: emptyTranscript(),
        }}
        onSessionEvent={onSessionEvent}
      />,
    );

    const textbox = await screen.findByRole("textbox", { name: "Message" });
    await user.type(textbox, "same draft");
    await user.keyboard("{Meta>}{Enter}{/Meta}");
    await selectSession(user, sessionB.title);
    await user.type(screen.getByRole("textbox", { name: "Message" }), "same draft");
    await act(async () => {
      delivery.resolve();
      await delivery.promise;
    });

    expect(screen.getByRole("textbox", { name: "Message" })).toHaveValue("same draft");
    expect(screen.queryByRole("status", { name: "Message status" })).not.toBeInTheDocument();
    await selectSession(user, sessionA.title);
    expect(screen.getByRole("textbox", { name: "Message" })).toHaveValue("");
  });

  it("preserves session A's same-text re-edit when its older delivery resolves across App switches", async () => {
    const user = userEvent.setup();
    const delivery = deferred();
    const sessionA = session({
      session_id: "delivery-stale-a",
      title: "Delivery stale A",
      workspace: "/work/delivery-stale-a",
    });
    const sessionB = session({
      session_id: "delivery-stale-b",
      title: "Delivery stale B",
      workspace: "/work/delivery-stale-b",
    });
    render(
      <App
        client={clientWithSessions([sessionA, sessionB])}
        sidebarStorage={storage}
        draftStorage={storage}
        transcriptBySession={{
          [sessionA.session_id]: emptyTranscript(),
          [sessionB.session_id]: emptyTranscript(),
        }}
        onSessionEvent={(sessionId) =>
          sessionId === sessionA.session_id ? delivery.promise : undefined}
      />,
    );

    const textbox = await screen.findByRole("textbox", { name: "Message" });
    await user.type(textbox, "same A text");
    await user.keyboard("{Meta>}{Enter}{/Meta}");
    await selectSession(user, sessionB.title);
    await user.type(screen.getByRole("textbox", { name: "Message" }), "B draft");
    await selectSession(user, sessionA.title);
    await user.clear(screen.getByRole("textbox", { name: "Message" }));
    await user.type(screen.getByRole("textbox", { name: "Message" }), "different A text");
    await user.clear(screen.getByRole("textbox", { name: "Message" }));
    await user.type(screen.getByRole("textbox", { name: "Message" }), "same A text");
    await selectSession(user, sessionB.title);
    await act(async () => {
      delivery.resolve();
      await delivery.promise;
    });

    expect(screen.getByRole("textbox", { name: "Message" })).toHaveValue("B draft");
    await selectSession(user, sessionA.title);
    expect(screen.getByRole("textbox", { name: "Message" })).toHaveValue("same A text");
  });

  it("returns an exact failed Plan delivery retry only to session A", async () => {
    const user = userEvent.setup();
    const delivery = deferred();
    let deliveryAttempts = 0;
    const sessionA = session({
      session_id: "delivery-failure-a",
      title: "Delivery failure A",
      workspace: "/work/delivery-failure-a",
    });
    const sessionB = session({
      session_id: "delivery-failure-b",
      title: "Delivery failure B",
      workspace: "/work/delivery-failure-b",
    });
    const onSessionEvent = vi.fn((sessionId: string) => {
      if (sessionId !== sessionA.session_id) return;
      deliveryAttempts += 1;
      return deliveryAttempts === 1 ? delivery.promise : undefined;
    });
    render(
      <App
        client={clientWithSessions([sessionA, sessionB])}
        sidebarStorage={storage}
        draftStorage={storage}
        transcriptBySession={{
          [sessionA.session_id]: emptyTranscript(),
          [sessionB.session_id]: emptyTranscript(),
        }}
        onSessionEvent={onSessionEvent}
      />,
    );

    const textbox = await screen.findByRole("textbox", { name: "Message" });
    await user.type(textbox, "exact A plan");
    await user.click(screen.getByRole("button", { name: "Plan mode" }));
    await user.click(screen.getByRole("button", { name: "Send message" }));
    await selectSession(user, sessionB.title);
    await user.type(screen.getByRole("textbox", { name: "Message" }), "B avoids A feedback");
    await act(async () => {
      delivery.reject(new Error("offline"));
      await delivery.promise.catch(() => {});
    });

    expect(screen.getByRole("textbox", { name: "Message" })).toHaveValue("B avoids A feedback");
    expect(screen.queryByRole("status", { name: "Message status" })).not.toBeInTheDocument();
    await selectSession(user, sessionA.title);
    expect(screen.getByRole("status", { name: "Message status" })).toHaveTextContent(
      "Message not sent",
    );
    await user.click(screen.getByRole("button", { name: "Retry message" }));
    expect(onSessionEvent).toHaveBeenLastCalledWith(sessionA.session_id, expect.objectContaining({
      type: "send_message",
      text: "exact A plan",
      mode: "plan",
    }));
    expect(deliveryAttempts).toBe(2);
  });

  it("keeps a deferred Stop failure with session A while session B is active", async () => {
    const user = userEvent.setup();
    const stop = deferred();
    let stopAttempts = 0;
    const sessionA = session({
      session_id: "stop-failure-a",
      title: "Stop failure A",
      workspace: "/work/stop-failure-a",
    });
    const sessionB = session({
      session_id: "stop-failure-b",
      title: "Stop failure B",
      workspace: "/work/stop-failure-b",
    });
    const onStopSession = vi.fn((sessionId: string) => {
      if (sessionId !== sessionA.session_id) return;
      stopAttempts += 1;
      return stopAttempts === 1 ? stop.promise : undefined;
    });
    render(
      <App
        client={clientWithSessions([sessionA, sessionB])}
        sidebarStorage={storage}
        draftStorage={storage}
        transcriptBySession={{
          [sessionA.session_id]: { ...emptyTranscript(), running: true },
          [sessionB.session_id]: { ...emptyTranscript(), running: true },
        }}
        onStopSession={onStopSession}
      />,
    );

    await screen.findByRole("textbox", { name: "Message" });
    await user.click(screen.getByRole("button", { name: "Stop turn" }));
    await selectSession(user, sessionB.title);
    await act(async () => {
      stop.reject(new Error("offline"));
      await stop.promise.catch(() => {});
    });

    expect(screen.getByRole("status", { name: "Turn status" })).toBeEmptyDOMElement();
    await selectSession(user, sessionA.title);
    expect(screen.getByRole("status", { name: "Turn status" })).toHaveTextContent(
      "Stop request failed",
    );
    await user.click(screen.getByRole("button", { name: "Stop turn" }));
    expect(onStopSession).toHaveBeenLastCalledWith(sessionA.session_id);
    expect(stopAttempts).toBe(2);
    expect(screen.getByRole("status", { name: "Turn status" })).toBeEmptyDOMElement();
  });

  it("keeps sidebar focus when switching from running session A to idle session B", async () => {
    const user = userEvent.setup();
    const sessionA = session({
      session_id: "running-focus-a",
      title: "Running focus A",
      workspace: "/work/running-focus-a",
    });
    const sessionB = session({
      session_id: "running-focus-b",
      title: "Running focus B",
      workspace: "/work/running-focus-b",
    });
    render(
      <App
        client={clientWithSessions([sessionA, sessionB])}
        sidebarStorage={storage}
        draftStorage={storage}
        transcriptBySession={{
          [sessionA.session_id]: { ...emptyTranscript(), running: true },
          [sessionB.session_id]: emptyTranscript(),
        }}
      />,
    );

    await screen.findByRole("textbox", { name: "Message" });
    await selectSession(user, sessionB.title);

    expect(screen.getByRole("button", { name: /^Running focus B,/i })).toHaveFocus();
    expect(screen.getByRole("textbox", { name: "Message" })).not.toHaveFocus();
  });

  it("keeps sidebar focus when session A's clear succeeds after switching to session B", async () => {
    const user = userEvent.setup();
    const clear = deferred();
    const sessionA = session({
      session_id: "clear-focus-a",
      title: "Clear focus A",
      workspace: "/work/clear-focus-a",
    });
    const sessionB = session({
      session_id: "clear-focus-b",
      title: "Clear focus B",
      workspace: "/work/clear-focus-b",
    });
    const initialTranscripts = {
      [sessionA.session_id]: {
        ...emptyTranscript(),
        running: true,
        queued: { type: "queue_message" as const, text: "focus-safe queue", mode: "base" as const },
      },
      [sessionB.session_id]: { ...emptyTranscript(), running: true },
    };
    const appProps = {
      client: clientWithSessions([sessionA, sessionB]),
      sidebarStorage: storage,
      draftStorage: storage,
      onSessionEvent: (sessionId: string, event: ClientEvent) =>
        sessionId === sessionA.session_id && event.type === "clear_queued_message"
          ? clear.promise
          : undefined,
    };
    const { rerender } = render(<App {...appProps} transcriptBySession={initialTranscripts} />);

    await screen.findByRole("textbox", { name: "Message" });
    await user.click(screen.getByRole("button", { name: "Edit queued follow-up" }));
    await selectSession(user, sessionB.title);
    rerender(
      <App
        {...appProps}
        transcriptBySession={{
          ...initialTranscripts,
          [sessionA.session_id]: { ...initialTranscripts[sessionA.session_id], queued: null },
        }}
      />,
    );
    await act(async () => {
      clear.resolve();
      await clear.promise;
    });

    expect(screen.getByRole("button", { name: /^Clear focus B,/i })).toHaveFocus();
    expect(screen.getByRole("textbox", { name: "Message" })).not.toHaveFocus();
  });
});
