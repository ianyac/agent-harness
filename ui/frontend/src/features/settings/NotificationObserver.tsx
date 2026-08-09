import { useEffect, useRef } from "react";

import type { PlatformAdapter } from "../../platform/types";
import type { TranscriptState } from "../../protocol/types";
import type { SessionRecord } from "../sessions/useSessions";
import type { Preferences } from "./preferences";

type NotificationObserverProps = {
  readonly clientKey: unknown;
  readonly platform: PlatformAdapter;
  readonly sessions: readonly SessionRecord[];
  readonly activeSessionId: string | null;
  readonly transcriptBySession: Readonly<Record<string, TranscriptState>>;
  readonly preferences: Preferences;
};

type Observation = {
  readonly generation: number;
  readonly sequence: number;
  readonly permissionId: string | null;
  readonly running: boolean;
};

function observation(state: TranscriptState): Observation {
  return {
    generation: state.generation,
    sequence: state.lastSequence,
    permissionId: state.permission?.requestId ?? null,
    running: state.running,
  };
}

function workspaceName(path: string): string {
  return path.split("/").filter(Boolean).at(-1) ?? path;
}

export function NotificationObserver({
  clientKey,
  platform,
  sessions,
  activeSessionId,
  transcriptBySession,
  preferences,
}: NotificationObserverProps) {
  const ownerRef = useRef<unknown>(Symbol("uninitialized"));
  const observedRef = useRef(new Map<string, Observation>());

  useEffect(() => {
    if (ownerRef.current !== clientKey) {
      ownerRef.current = clientKey;
      observedRef.current = new Map(
        sessions.flatMap((session) => {
          const state = transcriptBySession[session.session_id];
          return state === undefined ? [] : [[session.session_id, observation(state)] as const];
        }),
      );
      return;
    }

    const knownIds = new Set(sessions.map((session) => session.session_id));
    for (const sessionId of observedRef.current.keys()) {
      if (!knownIds.has(sessionId)) observedRef.current.delete(sessionId);
    }

    for (const session of sessions) {
      const state = transcriptBySession[session.session_id];
      if (state === undefined) continue;
      const current = observation(state);
      const previous = observedRef.current.get(session.session_id);
      if (previous === undefined || current.generation > previous.generation) {
        observedRef.current.set(session.session_id, current);
        continue;
      }
      if (
        current.generation < previous.generation
        || current.sequence < previous.sequence
      ) continue;

      observedRef.current.set(session.session_id, current);
      if (session.session_id === activeSessionId) continue;

      const body = `${workspaceName(session.workspace)} · ${session.workspace}`;
      if (
        preferences.notifyPermissions
        && current.permissionId !== null
        && current.permissionId !== previous.permissionId
      ) {
        void platform.notify({
          title: `Permission needed · ${session.title}`,
          body,
        }).catch(() => {});
      }
      if (preferences.notifyCompletions && previous.running && !current.running) {
        void platform.notify({
          title: `Work complete · ${session.title}`,
          body,
        }).catch(() => {});
      }
    }
  }, [activeSessionId, clientKey, platform, preferences, sessions, transcriptBySession]);

  return null;
}
