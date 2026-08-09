import { useCallback, useEffect, useState } from "react";

import type { BaseMode } from "../../protocol/types";
import type { ContextMode } from "../sessions/useSessions";

export type AppearancePreference = "system" | "light" | "dark";

export type Preferences = {
  readonly appearance: AppearancePreference;
  readonly defaultMode: BaseMode;
  readonly contextMode: ContextMode;
  readonly notifyPermissions: boolean;
  readonly notifyCompletions: boolean;
  readonly sidebarCollapsed: boolean;
};

export type PreferenceChanges = Partial<Preferences>;
export type PreferenceStorage = Pick<Storage, "getItem" | "setItem">;

const preferenceKey = "agent-harness:preferences";
const appearances = new Set<AppearancePreference>(["system", "light", "dark"]);
const baseModes = new Set<BaseMode>(["default", "acceptAll", "readOnly"]);
const contextModes = new Set<ContextMode>(["compaction", "folding"]);

export const defaultPreferences: Preferences = Object.freeze({
  appearance: "system",
  defaultMode: "default",
  contextMode: "compaction",
  notifyPermissions: false,
  notifyCompletions: false,
  sidebarCollapsed: false,
});

function defaultStorage(): PreferenceStorage | undefined {
  try {
    return typeof window === "undefined" ? undefined : window.localStorage;
  } catch {
    return undefined;
  }
}

function isPreferences(value: unknown): value is Preferences & { version: 1 } {
  if (typeof value !== "object" || value === null || Array.isArray(value)) return false;
  const record = value as Record<string, unknown>;
  return record.version === 1
    && appearances.has(record.appearance as AppearancePreference)
    && baseModes.has(record.defaultMode as BaseMode)
    && contextModes.has(record.contextMode as ContextMode)
    && typeof record.notifyPermissions === "boolean"
    && typeof record.notifyCompletions === "boolean"
    && typeof record.sidebarCollapsed === "boolean";
}

export function readPreferences(storage: PreferenceStorage | undefined = defaultStorage()): Preferences {
  try {
    const stored = storage?.getItem(preferenceKey);
    if (stored === null || stored === undefined) return defaultPreferences;
    const value: unknown = JSON.parse(stored);
    if (!isPreferences(value)) return defaultPreferences;
    return {
      appearance: value.appearance,
      defaultMode: value.defaultMode,
      contextMode: value.contextMode,
      notifyPermissions: value.notifyPermissions,
      notifyCompletions: value.notifyCompletions,
      sidebarCollapsed: value.sidebarCollapsed,
    };
  } catch {
    return defaultPreferences;
  }
}

function persist(storage: PreferenceStorage | undefined, preferences: Preferences): void {
  try {
    storage?.setItem(preferenceKey, JSON.stringify({ version: 1, ...preferences }));
  } catch {
    // Preferences are a convenience; the current view remains authoritative.
  }
}

function applyAppearance(appearance: AppearancePreference): void {
  if (typeof document === "undefined") return;
  if (appearance === "system") document.documentElement.removeAttribute("data-theme");
  else document.documentElement.dataset.theme = appearance;
}

export function usePreferences(providedStorage?: PreferenceStorage) {
  const storage = providedStorage ?? defaultStorage();
  const [preferences, setPreferences] = useState(() => readPreferences(storage));

  useEffect(() => {
    applyAppearance(preferences.appearance);
  }, [preferences.appearance]);

  const update = useCallback((changes: PreferenceChanges) => {
    setPreferences((current) => {
      const next = { ...current, ...changes };
      persist(storage, next);
      return next;
    });
  }, [storage]);

  return { preferences, update } as const;
}
