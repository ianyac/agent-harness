import { describe, expect, it } from "vitest";

import { createTauriPlatform } from "./tauri";
import type { NativeInvoke } from "./tauri";

describe("Tauri recovery capabilities", () => {
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
