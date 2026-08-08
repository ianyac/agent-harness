import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ClientEvent, ServerEvent, SessionSnapshot } from "../protocol/types";
import { createTauriPlatform } from "../platform/tauri";
import type { BrowserEnvironment } from "../platform/browser";
import type { ServiceConnection } from "../platform/types";
import { ApiClient } from "./http";
import {
  SessionSocket,
  type SessionSocketWebSocket,
  type WebSocketFactory,
} from "./sessionSocket";

const apiToken = "a".repeat(43);
const replacementToken = "b".repeat(43);
const staticToken = "s".repeat(43);

const connection: ServiceConnection = {
  baseUrl: "http://127.0.0.1:49152",
  token: apiToken,
};

function snapshot(overrides: Partial<SessionSnapshot> = {}): SessionSnapshot {
  return {
    type: "session_snapshot",
    session_id: "s1",
    generation: 1,
    sequence: 1,
    turn_id: null,
    messages: [],
    running: false,
    queued_message: null,
    safety: {},
    ...overrides,
  };
}

function delta(overrides: Partial<Extract<ServerEvent, { type: "assistant_delta" }>> = {}) {
  return {
    type: "assistant_delta" as const,
    session_id: "s1",
    generation: 1,
    sequence: 2,
    turn_id: "t1",
    text: "delta",
    ...overrides,
  };
}

class FakeWebSocket implements SessionSocketWebSocket {
  readyState = 0;
  onopen: ((event: Event) => void) | null = null;
  onmessage: ((event: MessageEvent) => void) | null = null;
  onclose: ((event: CloseEvent) => void) | null = null;
  onerror: ((event: Event) => void) | null = null;
  readonly sent: string[] = [];
  readonly closeCalls: Array<{ code?: number; reason?: string }> = [];

  constructor(
    readonly url: string,
    readonly protocols: string[],
  ) {}

  open(): void {
    this.readyState = 1;
    this.onopen?.(new Event("open"));
  }

  receive(value: unknown): void {
    const data = typeof value === "string" ? value : JSON.stringify(value);
    this.onmessage?.(new MessageEvent("message", { data }));
  }

  receiveBinary(): void {
    this.onmessage?.(new MessageEvent("message", { data: new Uint8Array([1]) }));
  }

  serverClose(): void {
    this.readyState = 3;
    this.onclose?.(new CloseEvent("close"));
  }

  send(value: string): void {
    if (this.readyState !== 1) {
      throw new Error("socket is not open");
    }
    this.sent.push(value);
  }

  close(code?: number, reason?: string): void {
    this.readyState = 3;
    this.closeCalls.push({ code, reason });
  }
}

class FakeWebSockets {
  readonly instances: FakeWebSocket[] = [];

  readonly create: WebSocketFactory = (url, protocols) => {
    const socket = new FakeWebSocket(url, [...protocols]);
    this.instances.push(socket);
    return socket;
  };

  latest(): FakeWebSocket {
    const socket = this.instances.at(-1);
    if (socket === undefined) throw new Error("no fake socket was created");
    return socket;
  }
}

function openSession(sockets = new FakeWebSockets()): {
  session: SessionSocket;
  sockets: FakeWebSockets;
} {
  const session = new SessionSocket({
    connection,
    sessionId: "s1",
    createWebSocket: sockets.create,
  });
  session.connect();
  sockets.latest().open();
  return { session, sockets };
}

describe("SessionSocket", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("keeps the bearer out of the URL and sends the exact authenticated subprotocols", () => {
    const { session, sockets } = openSession();

    expect(sockets.latest().url).toBe("ws://127.0.0.1:49152/ws/sessions/s1");
    expect(sockets.latest().url).not.toContain(apiToken);
    expect(sockets.latest().protocols).toEqual(["harness-ui", apiToken]);

    session.dispose();
  });

  it("ignores all events from a socket superseded by a replacement", () => {
    const { session, sockets } = openSession();
    const first = sockets.latest();
    first.receive(snapshot({ generation: 1 }));

    session.connect();
    const second = sockets.latest();
    second.open();
    second.receive(snapshot({ generation: 2 }));

    first.receive(delta({ generation: 2, sequence: 2, text: "stale" }));
    first.receive("not-json");

    expect(session.getState().streamingText).toBe("");
    expect(session.getState().error).toBeNull();
    session.dispose();
  });

  it("reconnects exponentially without ever waiting more than ten seconds", () => {
    const { session, sockets } = openSession();
    let current = sockets.latest();

    for (const delay of [1_000, 2_000, 4_000, 8_000, 10_000, 10_000]) {
      const before = sockets.instances.length;
      current.serverClose();
      vi.advanceTimersByTime(delay - 1);
      expect(sockets.instances).toHaveLength(before);
      vi.advanceTimersByTime(1);
      expect(sockets.instances).toHaveLength(before + 1);
      current = sockets.latest();
    }

    session.dispose();
  });

  it("cancels a pending reconnect when explicitly disposed", () => {
    const { session, sockets } = openSession();
    sockets.latest().serverClose();

    session.dispose();
    vi.advanceTimersByTime(60_000);

    expect(sockets.instances).toHaveLength(1);
  });

  it("closes the active socket and ignores its late events when disposed", () => {
    const { session, sockets } = openSession();
    const active = sockets.latest();

    session.dispose();
    active.receive(snapshot({ generation: 9 }));

    expect(active.closeCalls).toEqual([{ code: 1000, reason: "Client disposed" }]);
    expect(session.getState().generation).toBe(1);
    expect(session.getState().lastSequence).toBe(0);
  });

  it("buffers outbound events only until the authoritative snapshot arrives", () => {
    const { session, sockets } = openSession();
    const active = sockets.latest();
    const first: ClientEvent = { type: "send_message", text: "hello", mode: "base" };
    const second: ClientEvent = { type: "cancel_turn", turn_id: "t1" };

    session.send(first);
    expect(active.sent).toEqual([]);

    active.receive(snapshot());
    expect(active.sent).toEqual([JSON.stringify(first)]);

    session.send(second);
    expect(active.sent).toEqual([JSON.stringify(first), JSON.stringify(second)]);
    session.dispose();
  });

  it("keeps a reentrant replacement gated until that socket receives its own snapshot", () => {
    const { session, sockets } = openSession();
    const first = sockets.latest();
    const outbound: ClientEvent = { type: "send_message", text: "after replace", mode: "base" };
    let replaced = false;
    session.subscribe(() => {
      if (replaced) return;
      replaced = true;
      session.connect();
      sockets.latest().open();
    });

    first.receive(snapshot({ generation: 1 }));
    const replacement = sockets.latest();
    session.send(outbound);

    expect(replacement.sent).toEqual([]);
    replacement.receive(snapshot({ generation: 2 }));
    expect(replacement.sent).toEqual([JSON.stringify(outbound)]);
    session.dispose();
  });

  it("isolates a throwing subscriber while accepting a snapshot and flushing outbound events", () => {
    const { session, sockets } = openSession();
    const active = sockets.latest();
    const observedSequences: number[] = [];
    const outbound: ClientEvent = { type: "send_message", text: "still send", mode: "base" };
    session.subscribe(() => {
      throw new Error("consumer failed");
    });
    session.subscribe((state) => {
      observedSequences.push(state.lastSequence);
    });
    session.send(outbound);

    expect(() => active.receive(snapshot())).not.toThrow();
    expect(observedSequences).toEqual([1]);
    expect(active.sent).toEqual([JSON.stringify(outbound)]);
    session.dispose();
  });

  it("reconnects after a protocol error even when a subscriber throws", () => {
    const { session, sockets } = openSession();
    const active = sockets.latest();
    active.receive(snapshot());
    session.subscribe(() => {
      throw new Error("consumer failed");
    });

    expect(() => active.receiveBinary()).not.toThrow();
    vi.advanceTimersByTime(1_000);

    expect(sockets.instances).toHaveLength(2);
    session.dispose();
  });

  it("surfaces malformed server events as a recoverable protocol error", () => {
    const { session, sockets } = openSession();

    sockets.latest().receiveBinary();

    expect(session.getState().error).toEqual({
      category: "protocol",
      message: "The local service sent an invalid event. Reconnecting.",
    });
    session.dispose();
  });
});

describe("ApiClient", () => {
  it("sends the in-memory bearer without cookies, referrers, or URL credentials", async () => {
    let observed: Request | undefined;
    const fetchRequest: typeof fetch = async (input, init) => {
      observed = new Request(input, init);
      return new Response(JSON.stringify({ status: "ok" }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    };
    const client = new ApiClient(connection, fetchRequest);

    await expect(client.get<{ status: string }>("/api/health")).resolves.toEqual({
      status: "ok",
    });

    expect(observed?.headers.get("Authorization")).toBe(`Bearer ${apiToken}`);
    expect(observed?.credentials).toBe("omit");
    expect(observed?.referrerPolicy).toBe("no-referrer");
    expect(observed?.url).toBe("http://127.0.0.1:49152/api/health");
    expect(observed?.url).not.toContain(apiToken);
  });

  it("refuses to forward the bearer to a different origin", async () => {
    let fetched = false;
    const fetchRequest: typeof fetch = async () => {
      fetched = true;
      return new Response();
    };
    const client = new ApiClient(connection, fetchRequest);

    await expect(client.get("https://example.test/collect")).rejects.toThrow(
      "same service origin",
    );
    expect(fetched).toBe(false);
  });
});

function browserEnvironment(url: string): {
  environment: BrowserEnvironment;
  replaced: string[];
} {
  const current = new URL(url);
  const replaced: string[] = [];
  const environment: BrowserEnvironment = {
    location: current,
    history: {
      state: { navigation: "kept" },
      replaceState(data, _unused, nextUrl) {
        expect(data).toEqual({ navigation: "kept" });
        replaced.push(String(nextUrl));
        current.hash = "";
      },
    },
  };
  return { environment, replaced };
}

describe("browser platform", () => {
  beforeEach(() => {
    vi.resetModules();
  });

  it("captures the fragment once and immediately removes only that fragment", async () => {
    const { createBrowserPlatform } = await import("../platform/browser");
    const { environment, replaced } = browserEnvironment(
      `http://127.0.0.1:49152/_app/${staticToken}/?panel=activity#token=${apiToken}`,
    );

    const first = createBrowserPlatform(environment);
    expect(replaced).toEqual([`/_app/${staticToken}/?panel=activity`]);
    await expect(first.getServiceConnection()).resolves.toEqual(connection);

    environment.location.hash = `#token=${replacementToken}`;
    const second = createBrowserPlatform(environment);
    await expect(second.getServiceConnection()).resolves.toEqual(connection);
    expect(replaced).toHaveLength(1);
  });

  it("removes an invalid fragment before rejecting it", async () => {
    const { createBrowserPlatform } = await import("../platform/browser");
    const { environment, replaced } = browserEnvironment(
      `http://127.0.0.1:49152/_app/${staticToken}/#token=not-a-capability`,
    );

    expect(() => createBrowserPlatform(environment)).toThrow("API capability");
    expect(replaced).toEqual([`/_app/${staticToken}/`]);
    expect(environment.location.hash).toBe("");
  });
});

describe("Tauri platform", () => {
  it("obtains the connection once through the narrow command and retains it in memory", async () => {
    const commands: Array<{ command: string; args?: Record<string, unknown> }> = [];
    const invoke = async <T,>(command: string, args?: Record<string, unknown>): Promise<T> => {
      commands.push({ command, args });
      if (command === "service_connection") return connection as T;
      throw new Error(`unexpected command: ${command}`);
    };
    const platform = createTauriPlatform(invoke);

    await expect(platform.getServiceConnection()).resolves.toEqual(connection);
    await expect(platform.getServiceConnection()).resolves.toEqual(connection);
    expect(commands).toEqual([{ command: "service_connection", args: undefined }]);
  });

  it("uses only approved native commands and fails closed for general path reveal", async () => {
    const commands: Array<{ command: string; args?: Record<string, unknown> }> = [];
    const invoke = async <T,>(command: string, args?: Record<string, unknown>): Promise<T> => {
      commands.push({ command, args });
      if (command === "choose_workspace") return "/canonical/workspace" as T;
      return undefined as T;
    };
    const platform = createTauriPlatform(invoke);

    await expect(platform.chooseWorkspace()).resolves.toBe("/canonical/workspace");
    await platform.notify({ title: "Turn complete", body: "The answer is ready." });
    await expect(platform.revealPath("/tmp/not-approved")).rejects.toThrow(
      "Path reveal is not available",
    );

    expect(commands).toEqual([
      { command: "choose_workspace", args: undefined },
      {
        command: "notify",
        args: { title: "Turn complete", body: "The answer is ready." },
      },
    ]);
  });
});
