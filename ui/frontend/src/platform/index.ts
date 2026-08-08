import { createBrowserPlatform } from "./browser";
import { tauriPlatform } from "./tauri";
import type { PlatformAdapter } from "./types";

declare global {
  interface Window {
    __TAURI_INTERNALS__?: unknown;
  }
}

export type { PlatformAdapter, ServiceConnection } from "./types";

export const isTauri =
  typeof window !== "undefined" && window.__TAURI_INTERNALS__ !== undefined;

function selectPlatform(): PlatformAdapter {
  if (isTauri) return tauriPlatform;
  if (typeof window === "undefined") {
    throw new Error("The platform adapter requires a browser environment.");
  }
  return createBrowserPlatform(window);
}

export const platform = selectPlatform();
