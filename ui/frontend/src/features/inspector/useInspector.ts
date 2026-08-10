import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";

const WIDTH_KEY = "agent-harness:inspector-width";
const PIN_KEY_PREFIX = "agent-harness:inspector-pinned:";
export const INSPECTOR_MIN_WIDTH = 320;
export const INSPECTOR_MAX_WIDTH = 640;
const DEFAULT_WIDTH = 420;

export type InspectorStorage = Pick<Storage, "getItem" | "setItem">;

type UseInspectorOptions = {
  readonly sessionId: string | null;
  readonly storage?: InspectorStorage;
};

export type InspectorModel = {
  readonly open: boolean;
  readonly pinned: boolean;
  readonly selectedActivityId: string | null;
  readonly width: number;
  readonly narrow: boolean;
  readonly openOverview: (origin?: HTMLElement | null) => void;
  readonly openActivity: (activityId: string, origin?: HTMLElement | null) => void;
  readonly selectActivity: (activityId: string) => void;
  readonly toggle: (origin?: HTMLElement | null) => void;
  readonly close: () => void;
  readonly setPinned: (pinned: boolean) => void;
  readonly setWidth: (width: number) => void;
  readonly resizeBy: (delta: number) => void;
};

function defaultStorage(): InspectorStorage | undefined {
  try {
    return typeof window === "undefined" ? undefined : window.localStorage;
  } catch {
    return undefined;
  }
}

function read(storage: InspectorStorage | undefined, key: string): string | null {
  try {
    return storage?.getItem(key) ?? null;
  } catch {
    return null;
  }
}

function write(storage: InspectorStorage | undefined, key: string, value: string): void {
  try {
    storage?.setItem(key, value);
  } catch {
    // Preferences are best-effort; the active inspector remains usable.
  }
}

export function clampInspectorWidth(value: number): number {
  if (!Number.isFinite(value)) return DEFAULT_WIDTH;
  return Math.min(INSPECTOR_MAX_WIDTH, Math.max(INSPECTOR_MIN_WIDTH, Math.round(value)));
}

function storedWidth(storage: InspectorStorage | undefined): number {
  const raw = read(storage, WIDTH_KEY);
  if (raw === null || raw.trim() === "") return DEFAULT_WIDTH;
  const parsed = Number(raw);
  return Number.isFinite(parsed) ? clampInspectorWidth(parsed) : DEFAULT_WIDTH;
}

function storedPin(storage: InspectorStorage | undefined, sessionId: string): boolean {
  return read(storage, `${PIN_KEY_PREFIX}${sessionId}`) === "true";
}

function useNarrowInspector(): boolean {
  const query = "(max-width: 720px)";
  const [narrow, setNarrow] = useState(() =>
    typeof window !== "undefined" && typeof window.matchMedia === "function"
      ? window.matchMedia(query).matches
      : false,
  );

  useEffect(() => {
    if (typeof window === "undefined" || typeof window.matchMedia !== "function") return;
    const media = window.matchMedia(query);
    const update = () => setNarrow(media.matches);
    update();
    media.addEventListener("change", update);
    return () => media.removeEventListener("change", update);
  }, []);

  return narrow;
}

export function useInspector({ sessionId, storage: providedStorage }: UseInspectorOptions): InspectorModel {
  const storage = providedStorage ?? defaultStorage();
  const [open, setOpen] = useState(() =>
    sessionId === null ? false : storedPin(storage, sessionId));
  const [pinned, setPinnedState] = useState(open);
  const [selectedActivityId, setSelectedActivityId] = useState<string | null>(null);
  const [width, setWidthState] = useState(() => storedWidth(storage));
  const narrow = useNarrowInspector();
  const previousSession = useRef(sessionId);
  const origin = useRef<HTMLElement | null>(null);
  const openMemory = useRef(new Map<string, boolean>());
  const pinMemory = useRef(new Map<string, boolean>());
  const selectionMemory = useRef(new Map<string, string | null>());

  useLayoutEffect(() => {
    const previous = previousSession.current;
    if (previous === sessionId) return;
    if (previous !== null) {
      openMemory.current.set(previous, open);
      pinMemory.current.set(previous, pinned);
      selectionMemory.current.set(previous, selectedActivityId);
    }
    previousSession.current = sessionId;
    origin.current = null;
    if (sessionId === null) {
      setOpen(false);
      setPinnedState(false);
      setSelectedActivityId(null);
      return;
    }
    const nextPinned = pinMemory.current.get(sessionId) ?? storedPin(storage, sessionId);
    setPinnedState(nextPinned);
    setOpen(nextPinned && (openMemory.current.get(sessionId) ?? true));
    setSelectedActivityId(selectionMemory.current.get(sessionId) ?? null);
  }, [open, pinned, selectedActivityId, sessionId, storage]);

  const captureOrigin = useCallback((next: HTMLElement | null | undefined) => {
    if (next?.isConnected) origin.current = next;
  }, []);

  const restoreOrigin = useCallback(() => {
    const target = origin.current;
    origin.current = null;
    if (target?.isConnected) target.focus();
  }, []);

  const close = useCallback(() => {
    if (sessionId !== null) {
      pinMemory.current.set(sessionId, false);
      openMemory.current.set(sessionId, false);
      write(storage, `${PIN_KEY_PREFIX}${sessionId}`, "false");
    }
    setPinnedState(false);
    setOpen(false);
    queueMicrotask(restoreOrigin);
  }, [restoreOrigin, sessionId, storage]);

  const openOverview = useCallback((nextOrigin?: HTMLElement | null) => {
    if (sessionId === null) return;
    captureOrigin(nextOrigin);
    selectionMemory.current.set(sessionId, null);
    openMemory.current.set(sessionId, true);
    setSelectedActivityId(null);
    setOpen(true);
  }, [captureOrigin, sessionId]);

  const openActivity = useCallback((activityId: string, nextOrigin?: HTMLElement | null) => {
    if (sessionId === null) return;
    captureOrigin(nextOrigin);
    selectionMemory.current.set(sessionId, activityId);
    openMemory.current.set(sessionId, true);
    setSelectedActivityId(activityId);
    setOpen(true);
  }, [captureOrigin, sessionId]);

  const selectActivity = useCallback((activityId: string) => {
    if (sessionId === null) return;
    selectionMemory.current.set(sessionId, activityId);
    setSelectedActivityId(activityId);
  }, [sessionId]);

  const toggle = useCallback((nextOrigin?: HTMLElement | null) => {
    if (open) close();
    else openOverview(nextOrigin);
  }, [close, open, openOverview]);

  const setPinned = useCallback((nextPinned: boolean) => {
    if (sessionId === null) return;
    pinMemory.current.set(sessionId, nextPinned);
    write(storage, `${PIN_KEY_PREFIX}${sessionId}`, String(nextPinned));
    setPinnedState(nextPinned);
  }, [sessionId, storage]);

  const setWidth = useCallback((nextWidth: number) => {
    const next = clampInspectorWidth(nextWidth);
    setWidthState(next);
    write(storage, WIDTH_KEY, String(next));
  }, [storage]);

  const resizeBy = useCallback((delta: number) => {
    setWidthState((current) => {
      const next = clampInspectorWidth(current + delta);
      write(storage, WIDTH_KEY, String(next));
      return next;
    });
  }, [storage]);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (
        sessionId === null ||
        !event.metaKey || !event.shiftKey || event.ctrlKey || event.altKey || event.repeat ||
        event.key.toLocaleLowerCase() !== "i"
      ) return;
      event.preventDefault();
      const active = document.activeElement instanceof HTMLElement ? document.activeElement : null;
      toggle(active);
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [sessionId, toggle]);

  return {
    open,
    pinned,
    selectedActivityId,
    width,
    narrow,
    openOverview,
    openActivity,
    selectActivity,
    toggle,
    close,
    setPinned,
    setWidth,
    resizeBy,
  };
}
