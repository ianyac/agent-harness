import { invoke as invokeTauri } from "@tauri-apps/api/core";

import type { PlatformAdapter, ServiceConnection } from "./types";
import { normalizeServiceConnection } from "./types";

export type NativeInvoke = <T>(
  command: string,
  args?: Record<string, unknown>,
) => Promise<T>;

const defaultInvoke: NativeInvoke = (command, args) => invokeTauri(command, args);

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

export function createTauriPlatform(invoke: NativeInvoke = defaultInvoke): PlatformAdapter {
  let connectionRequest: Promise<ServiceConnection> | undefined;

  return Object.freeze({
    kind: "tauri" as const,
    getServiceConnection(): Promise<ServiceConnection> {
      connectionRequest ??= invoke<unknown>("service_connection").then(
        normalizeServiceConnection,
      );
      return connectionRequest;
    },
    async chooseWorkspace(): Promise<string | null> {
      return validateWorkspace(await invoke<unknown>("choose_workspace"));
    },
    async notify(input: { title: string; body: string }): Promise<void> {
      await invoke<void>("notify", { title: input.title, body: input.body });
    },
    async revealPath(_path: string): Promise<void> {
      throw new Error("Path reveal is not available through the approved native commands.");
    },
  });
}

export const tauriPlatform = createTauriPlatform();
