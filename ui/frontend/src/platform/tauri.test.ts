import { describe, expect, it } from "vitest";

import { createTauriPlatform } from "./tauri";
import type { NativeInvoke } from "./tauri";

describe("Tauri recovery capabilities", () => {
  it("retries service connection acquisition after a rejected invoke instead of caching the failure", async () => {
    let attempts = 0;
    const invoke: NativeInvoke = async <T,>(command: string) => {
      if (command !== "service_connection") return undefined as T;
      attempts += 1;
      if (attempts === 1) throw new Error("Sidecar is still starting");
      return {
        baseUrl: "http://127.0.0.1:4010",
        token: "t".repeat(43),
      } as T;
    };
    const platform = createTauriPlatform(invoke);

    await expect(platform.getServiceConnection()).rejects.toThrow("still starting");
    await expect(platform.getServiceConnection()).resolves.toEqual({
      baseUrl: "http://127.0.0.1:4010",
      token: "t".repeat(43),
    });
    expect(attempts).toBe(2);
  });

  it("maps fixed no-argument capabilities to fixed command names", async () => {
    const calls: Array<[string, Record<string, unknown>?]> = [];
    const invoke: NativeInvoke = async <T,>(command: string, args?: Record<string, unknown>) => {
      calls.push(args === undefined ? [command] : [command, args]);
      return undefined as T;
    };
    const platform = createTauriPlatform(invoke);

    await platform.restartService?.();
    await platform.openLogs?.();
    await platform.quit?.();

    expect(calls).toEqual([
      ["restart_service"],
      ["open_logs"],
      ["quit"],
    ]);
  });
});
