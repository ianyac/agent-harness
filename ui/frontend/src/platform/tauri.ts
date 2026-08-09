import { invoke as invokeTauri } from "@tauri-apps/api/core";
import { listen as listenTauri } from "@tauri-apps/api/event";

import type {
  PlatformAdapter,
  ServiceConnection,
  ServiceLifecycleState,
  ServiceLifecycleStatus,
} from "./types";
import { normalizeServiceConnection } from "./types";

export type NativeInvoke = <T>(
  command: string,
  args?: Record<string, unknown>,
) => Promise<T>;

export type NativeListen = (
  event: string,
  handler: (event: { payload: unknown }) => void,
) => Promise<() => void>;

export type ServiceStatePayload = ServiceLifecycleState;

type ReadyWaiter = {
  readonly resolve: (connection: ServiceConnection) => void;
  readonly reject: (error: Error) => void;
};

const SERVICE_STATE_EVENT = "service-state";
const SERVICE_UNAVAILABLE = "The local service is unavailable.";
const LISTENER_UNAVAILABLE = "The native service listener is unavailable.";
const READINESS_TIMEOUT_MS = 20_000;
const statuses = new Set<ServiceLifecycleStatus>([
  "starting",
  "ready",
  "restarting",
  "stopping",
  "stopped",
  "failed",
]);

const defaultInvoke: NativeInvoke = (command, args) => invokeTauri(command, args);
const defaultListen: NativeListen = (event, handler) => listenTauri(event, handler);

function exactKeys(record: Record<string, unknown>, expected: readonly string[]): boolean {
  const keys = Object.keys(record).sort();
  return keys.length === expected.length && keys.every((key, index) => key === expected[index]);
}

function normalizeServiceState(value: unknown): ServiceStatePayload | null {
  if (typeof value !== "object" || value === null || Array.isArray(value)) return null;
  const record = value as Record<string, unknown>;
  if (!Number.isSafeInteger(record.generation) || (record.generation as number) <= 0) return null;
  if (typeof record.status !== "string" || !statuses.has(record.status as ServiceLifecycleStatus)) return null;

  const generation = record.generation as number;
  const status = record.status as ServiceLifecycleStatus;
  if (status !== "ready") {
    if (!exactKeys(record, ["generation", "status"])) return null;
    return { generation, status };
  }
  if (!exactKeys(record, ["connection", "generation", "status"])) return null;
  if (typeof record.connection !== "object" || record.connection === null || Array.isArray(record.connection)) {
    return null;
  }
  if (!exactKeys(record.connection as Record<string, unknown>, ["baseUrl", "token"])) return null;
  try {
    return {
      generation,
      status,
      connection: normalizeServiceConnection(record.connection),
    };
  } catch {
    return null;
  }
}

function validateWorkspace(value: unknown): string | null {
  if (value === null) return null;
  if (
    typeof value !== "string" ||
    !value.startsWith("/") ||
    value.includes("\0") ||
    value.trim() !== value
  ) {
    throw new Error("The native host returned an invalid workspace path.");
  }
  return value;
}

export function createTauriPlatform(
  invoke: NativeInvoke = defaultInvoke,
  listen: NativeListen = defaultListen,
  readinessTimeoutMs = READINESS_TIMEOUT_MS,
): PlatformAdapter {
  let cachedConnection: ServiceConnection | undefined;
  let connectionRequest: Promise<ServiceConnection> | undefined;
  let listenerRequest: Promise<void> | undefined;
  let listenerRegistered = false;
  let latestState: ServiceStatePayload | undefined;
  let stateRevision = 0;
  const waiters = new Set<ReadyWaiter>();
  const subscribers = new Set<(state: ServiceLifecycleState) => void>();

  const hasTerminalState = () => latestState?.status === "failed" || latestState?.status === "stopped";

  const settleReady = (connection: ServiceConnection) => {
    for (const waiter of [...waiters]) waiter.resolve(connection);
    waiters.clear();
  };
  const settleUnavailable = () => {
    for (const waiter of [...waiters]) waiter.reject(new Error(SERVICE_UNAVAILABLE));
    waiters.clear();
  };
  const receiveState = (event: { payload: unknown }) => {
    const next = normalizeServiceState(event.payload);
    if (next === null || (latestState !== undefined && next.generation < latestState.generation)) {
      return;
    }
    latestState = next;
    stateRevision += 1;
    if (next.status === "ready") {
      cachedConnection = next.connection;
      settleReady(next.connection);
    } else {
      cachedConnection = undefined;
      if (next.status === "failed" || next.status === "stopped") settleUnavailable();
    }
    for (const subscriber of subscribers) {
      try {
        subscriber(next);
      } catch {
        // A view subscriber cannot interrupt service lifecycle authority.
      }
    }
  };
  const ensureListener = (): Promise<void> => {
    if (listenerRegistered) return Promise.resolve();
    listenerRequest ??= listen(SERVICE_STATE_EVENT, receiveState)
      .then(() => {
        listenerRegistered = true;
      })
      .catch(() => {
        listenerRequest = undefined;
        throw new Error(LISTENER_UNAVAILABLE);
      });
    return listenerRequest;
  };
  const waitForReady = () => {
    let waiter: ReadyWaiter;
    let timeout: ReturnType<typeof setTimeout>;
    const promise = new Promise<ServiceConnection>((resolve, reject) => {
      const finishResolve = (connection: ServiceConnection) => {
        clearTimeout(timeout);
        waiters.delete(waiter);
        resolve(connection);
      };
      const finishReject = (error: Error) => {
        clearTimeout(timeout);
        waiters.delete(waiter);
        reject(error);
      };
      waiter = { resolve: finishResolve, reject: finishReject };
      waiters.add(waiter);
      timeout = setTimeout(() => finishReject(new Error(SERVICE_UNAVAILABLE)), readinessTimeoutMs);
    });
    return {
      promise,
      cancel() {
        clearTimeout(timeout);
        waiters.delete(waiter);
      },
    };
  };

  return Object.freeze({
    kind: "tauri" as const,
    getServiceConnection(): Promise<ServiceConnection> {
      if (cachedConnection !== undefined) return Promise.resolve(cachedConnection);
      if (connectionRequest !== undefined) return connectionRequest;

      const pending = (async () => {
        await ensureListener();
        if (cachedConnection !== undefined) return cachedConnection;
        if (hasTerminalState()) {
          throw new Error(SERVICE_UNAVAILABLE);
        }

        const revisionAtInvoke = stateRevision;
        const eventWait = waitForReady();
        const invoked = invoke<unknown>("service_connection").then(
          (value) => ({ kind: "response" as const, value }),
          () => ({ kind: "unavailable" as const }),
        );
        const first = await Promise.race([
          invoked,
          eventWait.promise.then((connection) => ({ kind: "event" as const, connection })),
        ]);
        if (first.kind === "event") return first.connection;
        if (first.kind === "unavailable") return eventWait.promise;

        let connection: ServiceConnection;
        try {
          connection = normalizeServiceConnection(first.value);
        } catch (error) {
          eventWait.cancel();
          throw error;
        }
        if (stateRevision !== revisionAtInvoke) {
          if (cachedConnection !== undefined) {
            eventWait.cancel();
            return cachedConnection;
          }
          if (hasTerminalState()) {
            eventWait.cancel();
            throw new Error(SERVICE_UNAVAILABLE);
          }
          return eventWait.promise;
        }
        eventWait.cancel();
        cachedConnection = connection;
        return connection;
      })();
      const tracked = pending.finally(() => {
        if (connectionRequest === tracked) connectionRequest = undefined;
      });
      connectionRequest = tracked;
      return tracked;
    },
    subscribeServiceState(listener: (state: ServiceLifecycleState) => void): () => void {
      subscribers.add(listener);
      if (latestState !== undefined) {
        try {
          listener(latestState);
        } catch {
          // Replay is isolated from service lifecycle authority.
        }
      }
      void ensureListener().catch(() => {});
      return () => {
        subscribers.delete(listener);
      };
    },
    async chooseWorkspace(): Promise<string | null> {
      return validateWorkspace(await invoke<unknown>("choose_workspace"));
    },
    async notify(input: { title: string; body: string }): Promise<void> {
      await invoke<void>("notify", { title: input.title, body: input.body });
    },
    async openLogs(): Promise<void> {
      await invoke<void>("open_logs");
    },
    async restartService(): Promise<void> {
      normalizeServiceConnection(await invoke<unknown>("restart_service"));
    },
    async quit(): Promise<void> {
      await invoke<void>("quit_app");
    },
  });
}

export const tauriPlatform = createTauriPlatform();
