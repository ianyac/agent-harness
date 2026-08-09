import { describe, expect, it, vi } from "vitest";

import { createTauriPlatform } from "./tauri";
import type { NativeInvoke, NativeListen, ServiceStatePayload } from "./tauri";

const oldConnection = {
  baseUrl: "http://127.0.0.1:4010",
  token: "o".repeat(43),
};
const newConnection = {
  baseUrl: "http://127.0.0.1:49152",
  token: "n".repeat(43),
};

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((accept, decline) => {
    resolve = accept;
    reject = decline;
  });
  return { promise, resolve, reject };
}

function eventHarness() {
  let handler: ((event: { payload: unknown }) => void) | undefined;
  const listen: NativeListen = vi.fn(async (eventName, next) => {
    expect(eventName).toBe("service-state");
    handler = next;
    return () => {};
  });
  return {
    listen,
    emit(payload: ServiceStatePayload | unknown) {
      if (handler === undefined) throw new Error("listener is not registered");
      handler({ payload });
    },
  };
}

describe("Tauri native boundary", () => {
  it("registers the fixed state listener before requesting and normalizing connection details", async () => {
    const calls: string[] = [];
    const listen: NativeListen = async (eventName) => {
      calls.push(`listen:${eventName}`);
      return () => {};
    };
    const invoke: NativeInvoke = async <T,>(command: string) => {
      calls.push(`invoke:${command}`);
      return newConnection as T;
    };
    const platform = createTauriPlatform(invoke, listen);

    await expect(platform.getServiceConnection()).resolves.toEqual(newConnection);
    expect(calls).toEqual(["listen:service-state", "invoke:service_connection"]);
  });

  it("keeps one in-flight request and retries after a rejected normalized response", async () => {
    const response = deferred<unknown>();
    let attempts = 0;
    const invoke: NativeInvoke = async <T,>() => {
      attempts += 1;
      if (attempts === 1) return response.promise as Promise<T>;
      return newConnection as T;
    };
    const platform = createTauriPlatform(invoke, eventHarness().listen);

    const first = platform.getServiceConnection();
    const duplicate = platform.getServiceConnection();
    response.resolve({ baseUrl: "https://example.com", token: "bad" });

    await expect(first).rejects.toThrow("Invalid service API capability.");
    await expect(duplicate).rejects.toThrow("Invalid service API capability.");
    await expect(platform.getServiceConnection()).resolves.toEqual(newConnection);
    expect(attempts).toBe(2);
  });

  it("retries acquisition after an invoke failure reaches its bounded rejection", async () => {
    vi.useFakeTimers();
    try {
      let attempts = 0;
      const invoke: NativeInvoke = async <T,>() => {
        attempts += 1;
        if (attempts === 1) throw new Error("not ready");
        return newConnection as T;
      };
      const platform = createTauriPlatform(invoke, eventHarness().listen, 25);

      const first = platform.getServiceConnection();
      const rejection = expect(first).rejects.toThrow("The local service is unavailable.");
      await vi.advanceTimersByTimeAsync(25);
      await rejection;
      await expect(platform.getServiceConnection()).resolves.toEqual(newConnection);
      expect(attempts).toBe(2);
    } finally {
      vi.useRealTimers();
    }
  });

  it("waits through startup and resolves when a valid Ready event wins the invoke race", async () => {
    const events = eventHarness();
    const invoke: NativeInvoke = vi.fn(async () => {
      throw new Error("not ready");
    });
    const platform = createTauriPlatform(invoke, events.listen);

    const connection = platform.getServiceConnection();
    await vi.waitFor(() => expect(invoke).toHaveBeenCalledWith("service_connection"));
    events.emit({ generation: 1, status: "starting" });
    events.emit({ generation: 1, status: "ready", connection: newConnection });

    await expect(connection).resolves.toEqual(newConnection);
  });

  it("bounds readiness waiting when neither the command nor state events become ready", async () => {
    vi.useFakeTimers();
    try {
      const events = eventHarness();
      const invoke: NativeInvoke = async () => {
        throw new Error("not ready");
      };
      const platform = createTauriPlatform(invoke, events.listen, 25);

      const connection = platform.getServiceConnection();
      await Promise.resolve();
      await Promise.resolve();
      const rejection = expect(connection).rejects.toThrow("The local service is unavailable.");
      await vi.advanceTimersByTimeAsync(25);

      await rejection;
    } finally {
      vi.useRealTimers();
    }
  });

  it("replaces the cached connection on a newer Ready event and ignores stale generations", async () => {
    const events = eventHarness();
    const invokeSpy = vi.fn();
    const invoke: NativeInvoke = async <T,>(command: string) => {
      invokeSpy(command);
      return oldConnection as T;
    };
    const platform = createTauriPlatform(invoke, events.listen);

    await expect(platform.getServiceConnection()).resolves.toEqual(oldConnection);
    events.emit({ generation: 3, status: "ready", connection: newConnection });
    events.emit({ generation: 2, status: "ready", connection: oldConnection });

    await expect(platform.getServiceConnection()).resolves.toEqual(newConnection);
    expect(invokeSpy).toHaveBeenCalledTimes(1);
  });

  it("does not restore a stale invoke response after a newer non-Ready transition", async () => {
    const events = eventHarness();
    const response = deferred<unknown>();
    const invokeStarted = deferred<void>();
    const invoke: NativeInvoke = async <T,>() => {
      invokeStarted.resolve();
      return response.promise as Promise<T>;
    };
    const platform = createTauriPlatform(invoke, events.listen);

    const connection = platform.getServiceConnection();
    await invokeStarted.promise;
    events.emit({ generation: 2, status: "restarting" });
    response.resolve(oldConnection);
    const outcome = vi.fn();
    void connection.then(outcome, outcome);
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(outcome).not.toHaveBeenCalled();

    events.emit({ generation: 2, status: "ready", connection: newConnection });
    await expect(connection).resolves.toEqual(newConnection);
  });

  it.each(["failed", "stopped"] as const)(
    "clears stale connection and rejects readiness after %s",
    async (status) => {
      const events = eventHarness();
      const invokeSpy = vi.fn();
      const invoke: NativeInvoke = async <T,>(command: string) => {
        invokeSpy(command);
        return oldConnection as T;
      };
      const platform = createTauriPlatform(invoke, events.listen);
      await platform.getServiceConnection();

      events.emit({ generation: 4, status });

      await expect(platform.getServiceConnection()).rejects.toThrow(
        "The local service is unavailable.",
      );
      expect(invokeSpy).toHaveBeenCalledTimes(1);
    },
  );

  it.each(["starting", "restarting", "stopping"] as const)(
    "clears a stale cached connection while %s and waits for the replacement Ready event",
    async (status) => {
      const events = eventHarness();
      let requests = 0;
      const invoke: NativeInvoke = async <T,>() => {
        requests += 1;
        if (requests === 1) return oldConnection as T;
        throw new Error("not ready");
      };
      const platform = createTauriPlatform(invoke, events.listen);
      await platform.getServiceConnection();

      events.emit({ generation: 5, status });
      const replacement = platform.getServiceConnection();
      await Promise.resolve();
      events.emit({ generation: 5, status: "ready", connection: newConnection });

      await expect(replacement).resolves.toEqual(newConnection);
      expect(requests).toBe(2);
    },
  );

  it("ignores malformed state payloads without disclosing them", async () => {
    const events = eventHarness();
    const invoke: NativeInvoke = vi.fn(async () => {
      throw new Error("not ready");
    });
    const platform = createTauriPlatform(invoke, events.listen);
    const connection = platform.getServiceConnection();
    await vi.waitFor(() => expect(invoke).toHaveBeenCalled());

    events.emit({ generation: 1, status: "ready", connection: { ...newConnection, extra: "secret-detail" } });
    events.emit({ generation: 1, status: "invented", supplied: "secret-detail" });
    const outcome = vi.fn();
    void connection.then(outcome, outcome);
    await Promise.resolve();
    expect(outcome).not.toHaveBeenCalled();

    events.emit({ generation: 1, status: "ready", connection: newConnection });
    await expect(connection).resolves.toEqual(newConnection);
  });

  it("surfaces listener registration failure generically and retries registration", async () => {
    let attempts = 0;
    const listen: NativeListen = async () => {
      attempts += 1;
      if (attempts === 1) throw new Error("private listener detail");
      return () => {};
    };
    const invoke: NativeInvoke = async <T,>() => newConnection as T;
    const platform = createTauriPlatform(invoke, listen);

    await expect(platform.getServiceConnection()).rejects.toThrow(
      "The native service listener is unavailable.",
    );
    await expect(platform.getServiceConnection()).resolves.toEqual(newConnection);
    expect(attempts).toBe(2);
  });

  it("accepts cancellation and canonical absolute workspace results only", async () => {
    const responses: unknown[] = [null, "/canonical/workspace", "relative/workspace"];
    const calls: string[] = [];
    const invoke: NativeInvoke = async <T,>(command: string) => {
      calls.push(command);
      return responses.shift() as T;
    };
    const platform = createTauriPlatform(invoke, eventHarness().listen);

    await expect(platform.chooseWorkspace()).resolves.toBeNull();
    await expect(platform.chooseWorkspace()).resolves.toBe("/canonical/workspace");
    await expect(platform.chooseWorkspace()).rejects.toThrow("invalid workspace path");
    expect(calls).toEqual(["choose_workspace", "choose_workspace", "choose_workspace"]);
  });

  it("maps notification, logs, restart, and quit to exact narrow commands", async () => {
    const calls: Array<[string, Record<string, unknown>?]> = [];
    const invoke: NativeInvoke = async <T,>(command: string, args?: Record<string, unknown>) => {
      calls.push(args === undefined ? [command] : [command, args]);
      if (command === "restart_service") return newConnection as T;
      return undefined as T;
    };
    const platform = createTauriPlatform(invoke, eventHarness().listen);

    await platform.notify({ title: "Completed", body: "The agent finished." });
    await platform.openLogs?.();
    await platform.restartService?.();
    await platform.quit?.();

    expect(calls).toEqual([
      ["notify", { title: "Completed", body: "The agent finished." }],
      ["open_logs"],
      ["restart_service"],
      ["quit_app"],
    ]);
    expect(Object.keys(platform).sort()).toEqual([
      "chooseWorkspace",
      "getServiceConnection",
      "kind",
      "notify",
      "openLogs",
      "quit",
      "restartService",
    ]);
  });

  it("propagates fixed host notification rejection without widening its arguments", async () => {
    const invoke: NativeInvoke = vi.fn(async () => {
      throw new Error("Notification is unavailable.");
    });
    const platform = createTauriPlatform(invoke, eventHarness().listen);
    const title = "x".repeat(129);

    await expect(platform.notify({ title, body: "body" })).rejects.toThrow(
      "Notification is unavailable.",
    );
    expect(invoke).toHaveBeenCalledWith("notify", { title, body: "body" });
  });
});
