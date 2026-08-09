import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import type { ServiceConnection } from "../platform/types";
import { emptyTranscript } from "../protocol/reducer";
import type { ClientEvent, TranscriptState } from "../protocol/types";
import type { SessionRuntimeState } from "../features/sessions/useSessions";
import { SessionSocket, type SessionSocketLifecycle, type WebSocketFactory } from "./sessionSocket";

type SessionSocketModel = {
  readonly transcriptBySession: Readonly<Record<string, TranscriptState>>;
  readonly runtimeBySession: Readonly<Record<string, SessionRuntimeState>>;
  readonly send: (sessionId: string, event: ClientEvent) => void;
  readonly stop: (sessionId: string) => void;
  readonly lifecycleBySession: Readonly<Record<string, SessionSocketLifecycle>>;
  readonly reconnect: (sessionId: string) => void;
};

function runtime(state: TranscriptState): SessionRuntimeState {
  if (state.error !== null) return { status: "error" };
  if (state.stopping) return { status: "stopping" };
  if (state.permission !== null || state.planReview !== null) return { status: "waiting_permission" };
  if (state.running) return { status: "running" };
  if (state.terminal?.kind === "completed") return { status: "complete" };
  return { status: "idle" };
}

export function useSessionSockets(
  connection: ServiceConnection | null,
  sessionIds: readonly string[],
  createWebSocket?: WebSocketFactory,
): SessionSocketModel {
  const sockets = useRef(new Map<string, SessionSocket>());
  const [transcripts, setTranscripts] = useState<Readonly<Record<string, TranscriptState>>>({});
  const [lifecycles, setLifecycles] = useState<Readonly<Record<string, SessionSocketLifecycle>>>({});
  const sessionKey = sessionIds.join("\u0000");

  useEffect(() => () => {
    for (const socket of sockets.current.values()) socket.dispose();
    sockets.current.clear();
  }, [connection, createWebSocket]);

  useEffect(() => {
    const wanted = new Set(sessionIds);
    setTranscripts((current) => {
      const removed = Object.keys(current).filter((sessionId) => !wanted.has(sessionId));
      if (removed.length === 0) return current;
      const next = { ...current };
      for (const sessionId of removed) delete next[sessionId];
      return next;
    });
    setLifecycles((current) => {
      const removed = Object.keys(current).filter((sessionId) => !wanted.has(sessionId));
      if (removed.length === 0) return current;
      const next = { ...current };
      for (const sessionId of removed) delete next[sessionId];
      return next;
    });
    for (const [sessionId, socket] of sockets.current) {
      if (connection !== null && wanted.has(sessionId)) continue;
      socket.dispose();
      sockets.current.delete(sessionId);
      setTranscripts((current) => {
        if (!(sessionId in current)) return current;
        const next = { ...current };
        delete next[sessionId];
        return next;
      });
      setLifecycles((current) => {
        if (!(sessionId in current)) return current;
        const next = { ...current };
        delete next[sessionId];
        return next;
      });
    }
    if (connection === null) return;
    for (const sessionId of sessionIds) {
      if (sockets.current.has(sessionId)) continue;
      const socket = new SessionSocket({
        connection,
        sessionId,
        createWebSocket,
        onStateChange: (state) => {
          setTranscripts((current) => current[sessionId] === state
            ? current
            : { ...current, [sessionId]: state });
        },
        onLifecycleChange: (lifecycle) => {
          setLifecycles((current) => ({ ...current, [sessionId]: lifecycle }));
        },
      });
      sockets.current.set(sessionId, socket);
      setTranscripts((current) => ({ ...current, [sessionId]: socket.getState() }));
      setLifecycles((current) => ({ ...current, [sessionId]: "connecting" }));
      socket.connect();
    }
    // sessionKey is a stable value projection of the ordered session identities.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [connection, createWebSocket, sessionKey]);

  const send = useCallback((sessionId: string, event: ClientEvent) => {
    const socket = sockets.current.get(sessionId);
    if (socket === undefined) throw new Error("The session connection is not ready.");
    socket.send(event);
    if (event.type === "queue_message" || event.type === "clear_queued_message") {
      setTranscripts((current) => {
        const state = current[sessionId] ?? emptyTranscript();
        return {
          ...current,
          [sessionId]: {
            ...state,
            queued: event.type === "queue_message" ? event : null,
          },
        };
      });
    }
  }, []);

  const stop = useCallback((sessionId: string) => {
    const socket = sockets.current.get(sessionId);
    const turnId = socket?.getState().activeTurn?.turnId;
    if (socket === undefined || turnId === undefined) return;
    socket.send({ type: "cancel_turn", turn_id: turnId });
  }, []);

  const reconnect = useCallback((sessionId: string) => {
    sockets.current.get(sessionId)?.connect();
  }, []);

  const runtimeBySession = useMemo(() => Object.fromEntries(
    Object.entries(transcripts).map(([sessionId, state]) => [sessionId, runtime(state)]),
  ), [transcripts]);

  return {
    transcriptBySession: transcripts,
    runtimeBySession,
    lifecycleBySession: lifecycles,
    send,
    stop,
    reconnect,
  };
}
