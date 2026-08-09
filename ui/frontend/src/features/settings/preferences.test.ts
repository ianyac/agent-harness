import { cleanup, renderHook, act } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import {
  defaultPreferences,
  readPreferences,
  usePreferences,
} from "./preferences";

function memoryStorage(entries: Record<string, string> = {}) {
  const values = new Map(Object.entries(entries));
  return {
    values,
    getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => { values.set(key, value); },
  };
}

afterEach(() => {
  cleanup();
  document.documentElement.removeAttribute("data-theme");
});

describe("preferences", () => {
  it("falls back as one validated record when persisted JSON is malformed or out of schema", () => {
    const malformed = memoryStorage({ "agent-harness:preferences": "{" });
    expect(readPreferences(malformed)).toEqual(defaultPreferences);

    const invalid = memoryStorage({
      "agent-harness:preferences": JSON.stringify({
        version: 1,
        appearance: "neon",
        defaultMode: "acceptAll",
        contextMode: "both",
        notifyPermissions: "yes",
        notifyCompletions: true,
        sidebarCollapsed: false,
      }),
    });
    expect(readPreferences(invalid)).toEqual(defaultPreferences);
  });

  it("persists UI-only choices and truthfully applies or removes the root theme override", () => {
    const storage = memoryStorage();
    const { result } = renderHook(() => usePreferences(storage));

    act(() => result.current.update({ appearance: "dark", defaultMode: "readOnly" }));
    expect(document.documentElement).toHaveAttribute("data-theme", "dark");
    expect(JSON.parse(storage.values.get("agent-harness:preferences") ?? "null")).toEqual({
      version: 1,
      ...defaultPreferences,
      appearance: "dark",
      defaultMode: "readOnly",
    });
    expect(storage.values.get("agent-harness:preferences")).not.toMatch(/token|transcript|message/i);

    act(() => result.current.update({ appearance: "system" }));
    expect(document.documentElement).not.toHaveAttribute("data-theme");
  });
});
