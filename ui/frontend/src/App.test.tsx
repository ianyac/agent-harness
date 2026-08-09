import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { App } from "./App";
import { ApiClient } from "./api/http";
import type { SessionRecord } from "./features/sessions/useSessions";
import { event } from "./protocol/fixtures";
import { emptyTranscript, transcriptReducer } from "./protocol/reducer";
import type { ClientEvent } from "./protocol/types";

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

function deferred<T = void>() {
  let resolve!: (value: T | PromiseLike<T>) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((accept, decline) => {
    resolve = accept;
    reject = decline;
  });
  return { promise, resolve, reject };
}

function clientWith(fetchRequest: typeof fetch) {
  return new ApiClient(connection, fetchRequest);
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

    expect(onSessionEvent).toHaveBeenCalledWith("session-a", {
      type: "send_message",
      text: "routed message",
      mode: "base",
    });
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
    const permissionState = transcriptReducer(emptyTranscript(), event("permission_requested", {
      sequence: 1,
      request_id: "permission-a",
      action: "write_file",
      scope: '{"path":"A.md"}',
    }));
    const planState = transcriptReducer(emptyTranscript(), event("plan_approval_requested", {
      sequence: 1,
      request_id: "plan-b",
      plan: "1. Verify B",
    }));

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
    expect(onSessionEvent).toHaveBeenLastCalledWith(sessionA.session_id, {
      type: "send_message",
      text: "exact A plan",
      mode: "plan",
    });
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
