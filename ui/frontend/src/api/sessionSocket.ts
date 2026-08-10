import { parseServerEvent } from "../protocol/parse";
import { emptyTranscript, transcriptReducer } from "../protocol/reducer";
import type {
  ClearQueuedMessage,
  ClientEvent,
  QueuedMessage,
  ServerEvent,
  SessionSnapshot,
  TranscriptState,
} from "../protocol/types";
import type { ServiceConnection } from "../platform/types";
import { normalizeServiceConnection } from "../platform/types";

export interface SessionSocketWebSocket {
  readonly readyState: number;
  onopen: ((event: Event) => void) | null;
  onmessage: ((event: MessageEvent) => void) | null;
  onclose: ((event: CloseEvent) => void) | null;
  onerror: ((event: Event) => void) | null;
  send(data: string): void;
  close(code?: number, reason?: string): void;
}

export type WebSocketFactory = (
  url: string,
  protocols: readonly string[],
) => SessionSocketWebSocket;

type SessionSocketOptions = {
  readonly connection: ServiceConnection;
  readonly sessionId: string;
  readonly createWebSocket?: WebSocketFactory;
  readonly initialState?: TranscriptState;
  readonly onStateChange?: (state: TranscriptState) => void;
  readonly onLifecycleChange?: (lifecycle: SessionSocketLifecycle) => void;
};

export type SessionSocketLifecycle = "connecting" | "connected" | "reconnecting" | "failed";

const openReadyState = 1;
const reconnectMaximumMs = 10_000;
const protocolFailureLimit = 3;
const protocolError = {
  category: "protocol",
  message: "The local service sent an invalid event. Reconnecting.",
} as const;

const createNativeWebSocket: WebSocketFactory = (url, protocols) =>
  new WebSocket(url, [...protocols]);

function socketUrl(baseUrl: string, sessionId: string): string {
  if (sessionId.trim() === "") throw new Error("A session id is required.");
  let encodedSession: string;
  try {
    encodedSession = encodeURIComponent(sessionId);
  } catch {
    throw new Error("The session id is invalid.");
  }
  const url = new URL(baseUrl);
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  url.pathname = `/ws/sessions/${encodedSession}`;
  url.search = "";
  url.hash = "";
  return url.toString();
}

export class SessionSocket {
  readonly #connection: ServiceConnection;
  readonly #sessionId: string;
  readonly #url: string;
  readonly #createWebSocket: WebSocketFactory;
  readonly #listeners = new Set<(state: TranscriptState) => void>();
  readonly #outbound: string[] = [];
  readonly #onLifecycleChange?: (lifecycle: SessionSocketLifecycle) => void;
  #state: TranscriptState;
  #socket: SessionSocketWebSocket | undefined;
  #socketEpoch = 0;
  #snapshotReady = false;
  #reconnectAttempt = 0;
  #protocolFailures = 0;
  #stopRequested = false;
  #reconnectTimer: ReturnType<typeof setTimeout> | undefined;
  #disposed = false;
  #lifecycle: SessionSocketLifecycle = "connecting";

  constructor(options: SessionSocketOptions) {
    this.#connection = normalizeServiceConnection(options.connection);
    this.#sessionId = options.sessionId;
    this.#url = socketUrl(this.#connection.baseUrl, this.#sessionId);
    this.#createWebSocket = options.createWebSocket ?? createNativeWebSocket;
    this.#state = options.initialState ?? emptyTranscript();
    this.#onLifecycleChange = options.onLifecycleChange;
    if (options.onStateChange !== undefined) {
      this.#listeners.add(options.onStateChange);
    }
  }

  getState(): TranscriptState {
    return this.#state;
  }

  subscribe(listener: (state: TranscriptState) => void): () => void {
    if (this.#disposed) return () => {};
    this.#listeners.add(listener);
    return () => {
      this.#listeners.delete(listener);
    };
  }

  connect(): void {
    if (this.#disposed) throw new Error("The session socket is disposed.");
    this.#protocolFailures = 0;
    if (this.#lifecycle === "failed") this.#publishLifecycle("connecting");
    this.#clearReconnect();
    this.#openSocket();
  }

  applyLocal(event: QueuedMessage | ClearQueuedMessage): void {
    if (this.#disposed) return;
    this.#publish({
      ...this.#state,
      queued: event.type === "queue_message" ? event : null,
    });
  }

  requestStop(): void {
    if (this.#disposed) return;
    const turnId = this.#state.activeTurn?.turnId;
    if (turnId === undefined) {
      this.#stopRequested = true;
      return;
    }
    this.send({ type: "cancel_turn", turn_id: turnId });
  }

  send(event: ClientEvent): void {
    if (this.#disposed) throw new Error("The session socket is disposed.");
    const serialized = JSON.stringify(event);
    const socket = this.#socket;
    if (socket === undefined || !this.#snapshotReady || socket.readyState !== openReadyState) {
      this.#outbound.push(serialized);
      return;
    }
    try {
      socket.send(serialized);
    } catch {
      this.#outbound.push(serialized);
      this.#retireAndReconnect(socket, "Send failed");
    }
  }

  close(): void {
    this.dispose();
  }

  dispose(): void {
    if (this.#disposed) return;
    this.#disposed = true;
    this.#clearReconnect();
    this.#snapshotReady = false;
    this.#outbound.length = 0;
    this.#socketEpoch += 1;
    const socket = this.#socket;
    this.#socket = undefined;
    if (socket !== undefined) {
      this.#detach(socket);
      try {
        socket.close(1000, "Client disposed");
      } catch {
        // The socket has already released its native resource.
      }
    }
    this.#listeners.clear();
  }

  #openSocket(): void {
    const epoch = this.#socketEpoch + 1;
    this.#socketEpoch = epoch;
    this.#snapshotReady = false;
    // Each connection's snapshot must apply to a fresh baseline; a service
    // restart may legitimately reissue lower generations.
    this.#state = emptyTranscript();

    const previous = this.#socket;
    this.#socket = undefined;
    if (previous !== undefined) {
      this.#detach(previous);
      try {
        previous.close(1000, "Superseded");
      } catch {
        // Replacement authority has already moved to the next epoch.
      }
    }

    let socket: SessionSocketWebSocket;
    try {
      socket = this.#createWebSocket(this.#url, ["harness-ui", this.#connection.token]);
    } catch {
      this.#scheduleReconnect();
      return;
    }
    this.#socket = socket;
    socket.onopen = () => {};
    socket.onmessage = (event) => {
      if (this.#isCurrent(epoch, socket)) this.#receive(socket, event.data);
    };
    socket.onclose = () => {
      if (!this.#isCurrent(epoch, socket)) return;
      this.#detach(socket);
      this.#socket = undefined;
      this.#snapshotReady = false;
      this.#scheduleReconnect();
    };
    socket.onerror = () => {
      if (this.#isCurrent(epoch, socket)) this.#retireAndReconnect(socket, "Connection error");
    };
  }

  #receive(socket: SessionSocketWebSocket, data: unknown): void {
    if (typeof data !== "string") {
      this.#reportProtocolError(socket);
      return;
    }

    let input: unknown;
    try {
      input = JSON.parse(data) as unknown;
    } catch {
      this.#reportProtocolError(socket);
      return;
    }
    const parsed = parseServerEvent(input);
    if (!parsed.ok || parsed.value.session_id !== this.#sessionId) {
      this.#reportProtocolError(socket);
      return;
    }

    if (!this.#snapshotReady) {
      const next =
        parsed.value.type === "session_snapshot"
          ? this.#acceptedSnapshot(parsed.value)
          : undefined;
      if (next === undefined) {
        this.#reportProtocolError(socket);
        return;
      }
      this.#snapshotReady = true;
      this.#reconnectAttempt = 0;
      this.#protocolFailures = 0;
      this.#publishLifecycle("connected");
      this.#flush(socket);
      this.#publish(next);
      return;
    }

    this.#publish(transcriptReducer(this.#state, parsed.value));
    this.#applyStopIntent(parsed.value);
  }

  #applyStopIntent(event: ServerEvent): void {
    if (!this.#stopRequested) return;
    if (event.type === "turn_started" && this.#state.activeTurn?.turnId === event.turn_id) {
      this.#stopRequested = false;
      this.send({ type: "cancel_turn", turn_id: event.turn_id });
      return;
    }
    if (
      event.type === "turn_completed" ||
      event.type === "turn_cancelled" ||
      event.type === "turn_failed"
    ) {
      this.#stopRequested = false;
    }
  }

  #acceptedSnapshot(event: SessionSnapshot): TranscriptState | undefined {
    const next = transcriptReducer(this.#state, event);
    if (
      next === this.#state ||
      next.generation !== event.generation ||
      next.lastSequence !== event.sequence
    ) {
      return undefined;
    }
    return next;
  }

  #flush(socket: SessionSocketWebSocket): void {
    while (
      this.#outbound.length > 0 &&
      this.#socket === socket &&
      this.#snapshotReady &&
      socket.readyState === openReadyState
    ) {
      const next = this.#outbound[0];
      try {
        socket.send(next);
        this.#outbound.shift();
      } catch {
        this.#retireAndReconnect(socket, "Send failed");
        return;
      }
    }
  }

  #reportProtocolError(socket: SessionSocketWebSocket): void {
    this.#protocolFailures += 1;
    if (this.#protocolFailures >= protocolFailureLimit) {
      this.#retire(socket, "Protocol error");
      this.#publishLifecycle("failed");
    } else {
      this.#retireAndReconnect(socket, "Protocol error");
    }
    this.#publish({ ...this.#state, error: protocolError });
  }

  #publish(next: TranscriptState): void {
    if (next === this.#state) return;
    this.#state = next;
    for (const listener of this.#listeners) {
      try {
        listener(next);
      } catch {
        // A view subscriber cannot interrupt socket authority or cleanup.
      }
    }
  }

  #retire(socket: SessionSocketWebSocket, reason: string): void {
    if (this.#socket !== socket) return;
    this.#socketEpoch += 1;
    this.#socket = undefined;
    this.#snapshotReady = false;
    this.#detach(socket);
    try {
      socket.close(1000, reason);
    } catch {
      // Reconnect still proceeds if the native socket is already gone.
    }
  }

  #retireAndReconnect(socket: SessionSocketWebSocket, reason: string): void {
    if (this.#socket !== socket) return;
    this.#retire(socket, reason);
    this.#scheduleReconnect();
  }

  #scheduleReconnect(): void {
    if (this.#disposed || this.#reconnectTimer !== undefined) return;
    this.#publishLifecycle("reconnecting");
    const exponent = Math.min(this.#reconnectAttempt, 4);
    const delay = Math.min(1_000 * 2 ** exponent, reconnectMaximumMs);
    this.#reconnectAttempt += 1;
    this.#reconnectTimer = setTimeout(() => {
      this.#reconnectTimer = undefined;
      if (!this.#disposed) this.#openSocket();
    }, delay);
  }

  #clearReconnect(): void {
    if (this.#reconnectTimer === undefined) return;
    clearTimeout(this.#reconnectTimer);
    this.#reconnectTimer = undefined;
  }

  #publishLifecycle(next: SessionSocketLifecycle): void {
    if (this.#lifecycle === next) return;
    this.#lifecycle = next;
    this.#onLifecycleChange?.(next);
  }

  #isCurrent(epoch: number, socket: SessionSocketWebSocket): boolean {
    return !this.#disposed && epoch === this.#socketEpoch && socket === this.#socket;
  }

  #detach(socket: SessionSocketWebSocket): void {
    socket.onopen = null;
    socket.onmessage = null;
    socket.onclose = null;
    socket.onerror = null;
  }
}
