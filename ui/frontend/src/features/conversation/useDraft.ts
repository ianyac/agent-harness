import { useCallback, useEffect, useRef, useState } from "react";

export type DraftStorage = Pick<Storage, "getItem" | "setItem">;

type UseDraftOptions = {
  readonly storage?: DraftStorage;
  readonly backupDelayMs?: number;
  readonly memory?: Map<string, string>;
};

const inMemoryDrafts = new Map<string, string>();
const defaultBackupDelayMs = 300;
const storagePrefix = "agent-harness:draft:";

function defaultStorage(): DraftStorage | undefined {
  try {
    return typeof window === "undefined" ? undefined : window.localStorage;
  } catch {
    return undefined;
  }
}

function storageKey(sessionId: string): string {
  return `${storagePrefix}${encodeURIComponent(sessionId)}`;
}

function restoredText(sessionId: string, memory: Map<string, string>, storage?: DraftStorage): string {
  const inMemory = memory.get(sessionId);
  if (inMemory !== undefined) return inMemory;
  if (storage === undefined) return "";
  try {
    const raw = storage.getItem(storageKey(sessionId));
    if (raw === null) return "";
    const value = JSON.parse(raw) as unknown;
    if (
      value !== null &&
      !Array.isArray(value) &&
      typeof value === "object" &&
      typeof (value as { text?: unknown }).text === "string"
    ) {
      const text = (value as { text: string }).text;
      memory.set(sessionId, text);
      return text;
    }
  } catch {
    // A malformed or unavailable convenience backup is safe to ignore.
  }
  return "";
}

export function useDraft(sessionId: string, options: UseDraftOptions = {}) {
  const memory = options.memory ?? inMemoryDrafts;
  const storage = options.storage ?? defaultStorage();
  const backupDelayMs = options.backupDelayMs ?? defaultBackupDelayMs;
  const [state, setState] = useState(() => ({
    sessionId,
    text: restoredText(sessionId, memory, storage),
  }));
  const backupTimers = useRef(new Map<string, ReturnType<typeof setTimeout>>());
  const currentText = state.sessionId === sessionId
    ? state.text
    : restoredText(sessionId, memory, storage);
  const sessionRef = useRef(sessionId);
  sessionRef.current = sessionId;

  const scheduleBackup = useCallback((targetSessionId: string, text: string) => {
    if (storage === undefined) return;
    const existing = backupTimers.current.get(targetSessionId);
    if (existing !== undefined) clearTimeout(existing);
    const timer = setTimeout(() => {
      if (backupTimers.current.get(targetSessionId) !== timer) return;
      backupTimers.current.delete(targetSessionId);
      try {
        storage.setItem(storageKey(targetSessionId), JSON.stringify({ text }));
      } catch {
        // Drafts remain available in memory when preference storage is unavailable.
      }
    }, backupDelayMs);
    backupTimers.current.set(targetSessionId, timer);
  }, [backupDelayMs, storage]);

  useEffect(() => {
    if (state.sessionId !== sessionId) {
      setState({ sessionId, text: currentText });
    }
  }, [currentText, sessionId, state.sessionId]);

  useEffect(() => () => {
    for (const timer of backupTimers.current.values()) clearTimeout(timer);
    backupTimers.current.clear();
  }, [backupDelayMs, storage]);

  const setText = useCallback((next: string | ((current: string) => string)) => {
    const targetSessionId = sessionRef.current;
    const visible = restoredText(targetSessionId, memory, storage);
    const text = typeof next === "function" ? next(visible) : next;
    memory.set(targetSessionId, text);
    scheduleBackup(targetSessionId, text);
    setState({ sessionId: targetSessionId, text });
  }, [memory, scheduleBackup, storage]);

  const setSessionText = useCallback((targetSessionId: string, next: string | ((current: string) => string)) => {
    if (targetSessionId === sessionRef.current) {
      setText(next);
      return;
    }
    const visible = restoredText(targetSessionId, memory, storage);
    const text = typeof next === "function" ? next(visible) : next;
    memory.set(targetSessionId, text);
    scheduleBackup(targetSessionId, text);
  }, [memory, scheduleBackup, setText, storage]);

  return [currentText, setText, setSessionText] as const;
}
