import { act, renderHook } from "@testing-library/react";
import { expect, it } from "vitest";

import type { ServiceConnection } from "../platform/types";
import type { SessionSnapshot } from "../protocol/types";
import type { SessionSocketWebSocket, WebSocketFactory } from "./sessionSocket";
import { useSessionSockets } from "./useSessionSockets";

const connection: ServiceConnection = {
  baseUrl: "http://127.0.0.1:49152",
  token: "a".repeat(43),
};

class FakeWebSocket implements SessionSocketWebSocket {
  readyState = 1;
  onopen: ((event: Event) => void) | null = null;
  onmessage: ((event: MessageEvent) => void) | null = null;
  onclose: ((event: CloseEvent) => void) | null = null;
  onerror: ((event: Event) => void) | null = null;
  readonly closeCalls: Array<{ code?: number; reason?: string }> = [];
  readonly sent: string[] = [];

  constructor(readonly sessionId: string) {}

  send(value: string): void {
    this.sent.push(value);
  }

  close(code?: number, reason?: string): void {
    this.readyState = 3;
    this.closeCalls.push({ code, reason });
  }

  receive(value: unknown): void {
    this.onmessage?.(new MessageEvent("message", { data: JSON.stringify(value) }));
  }
}

it("stops the exact turn hydrated from a running reconnect snapshot", () => {
  const sockets: FakeWebSocket[] = [];
  const createWebSocket: WebSocketFactory = (url) => {
    const sessionId = url.split("/").at(-1);
    if (sessionId === undefined) throw new Error("Missing fixture session id");
    const socket = new FakeWebSocket(sessionId);
    sockets.push(socket);
    return socket;
  };
  const { result } = renderHook(() =>
    useSessionSockets(connection, ["resumed"], createWebSocket),
  );
  const socket = sockets[0]!;

  act(() => socket.receive({
    ...snapshot("resumed"),
    generation: 2,
    turn_id: "turn-from-snapshot",
    running: true,
  }));
  act(() => result.current.stop("resumed"));

  expect(socket.sent.map((value) => JSON.parse(value))).toContainEqual({
    type: "cancel_turn",
    turn_id: "turn-from-snapshot",
  });
});

function snapshot(sessionId: string): SessionSnapshot {
  return {
    type: "session_snapshot",
    session_id: sessionId,
    generation: 1,
    sequence: 1,
    turn_id: null,
    messages: [],
    running: false,
    queued_message: null,
    safety: {},
  };
}

it("retains a streaming session socket while adding another and removes only the archived session state", () => {
  const sockets: FakeWebSocket[] = [];
  const createWebSocket: WebSocketFactory = (url) => {
    const sessionId = url.split("/").at(-1);
    if (sessionId === undefined) throw new Error("Missing fixture session id");
    const socket = new FakeWebSocket(sessionId);
    sockets.push(socket);
    return socket;
  };
  const { result, rerender, unmount } = renderHook(
    ({ sessionIds }) => useSessionSockets(connection, sessionIds, createWebSocket),
    { initialProps: { sessionIds: ["streaming"] as readonly string[] } },
  );

  const retainedSocket = sockets[0];
  expect(retainedSocket).toBeDefined();
  act(() => {
    retainedSocket!.receive(snapshot("streaming"));
    retainedSocket!.receive({
      type: "turn_started",
      session_id: "streaming",
      generation: 1,
      sequence: 2,
      turn_id: "turn-streaming",
      mode: "base",
      submission_id: "submission-streaming",
    });
    retainedSocket!.receive({
      type: "assistant_delta",
      session_id: "streaming",
      generation: 1,
      sequence: 3,
      turn_id: "turn-streaming",
      text: "retained stream",
    });
  });

  rerender({ sessionIds: ["streaming", "archived"] });

  expect(sockets.filter((socket) => socket.sessionId === "streaming")).toEqual([retainedSocket]);
  expect(retainedSocket!.closeCalls).toEqual([]);
  expect(result.current.transcriptBySession.streaming?.streamingText).toBe("retained stream");
  const archivedSocket = sockets.find((socket) => socket.sessionId === "archived");
  expect(archivedSocket).toBeDefined();
  act(() => archivedSocket!.receive(snapshot("archived")));

  rerender({ sessionIds: ["archived", "streaming"] });
  expect(sockets).toHaveLength(2);
  expect(retainedSocket!.closeCalls).toEqual([]);

  rerender({ sessionIds: ["streaming"] });

  expect(retainedSocket!.closeCalls).toEqual([]);
  expect(archivedSocket!.closeCalls).toHaveLength(1);
  expect(result.current.transcriptBySession.streaming?.streamingText).toBe("retained stream");
  expect(result.current.transcriptBySession).not.toHaveProperty("archived");
  expect(result.current.lifecycleBySession).not.toHaveProperty("archived");

  unmount();
  expect(retainedSocket!.closeCalls).toHaveLength(1);
});
