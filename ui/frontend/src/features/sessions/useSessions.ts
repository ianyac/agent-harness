import { useCallback, useEffect, useState } from "react";

import type { ApiClient } from "../../api/http";
import type { BaseMode } from "../../protocol/types";

export type ContextMode = "compaction" | "folding";

export type SessionRecord = {
  readonly session_id: string;
  readonly workspace: string;
  readonly title: string;
  readonly mode: BaseMode;
  readonly context_mode: ContextMode;
  readonly created_at: string;
  readonly updated_at: string;
  readonly last_opened_at: string | null;
  readonly archived_at: string | null;
};

export type SessionRuntimeStatus =
  | "idle"
  | "running"
  | "waiting_permission"
  | "stopping"
  | "complete"
  | "error";

export type SessionRuntimeState = {
  readonly status: SessionRuntimeStatus;
};

type ServiceConfig = {
  readonly base_workspace: string;
  readonly default_mode: BaseMode;
  readonly default_context_mode: ContextMode;
  readonly modes: BaseMode[];
  readonly context_modes: ContextMode[];
};

export type SessionsModel = {
  readonly sessions: SessionRecord[];
  readonly activeSessionId: string | null;
  readonly loading: boolean;
  readonly error: Error | null;
  readonly createSession: () => Promise<void>;
  readonly selectSession: (sessionId: string) => void;
  readonly renameSession: (sessionId: string, title: string) => Promise<void>;
  readonly archiveSession: (sessionId: string) => Promise<void>;
  readonly refresh: () => Promise<void>;
};

function unarchived(records: readonly SessionRecord[]): SessionRecord[] {
  return records.filter((record) => record.archived_at === null);
}

function errorFrom(value: unknown): Error {
  return value instanceof Error ? value : new Error("The session operation failed.");
}

export function useSessions(client: ApiClient | null): SessionsModel {
  const [sessions, setSessions] = useState<SessionRecord[]>([]);
  const [config, setConfig] = useState<ServiceConfig | null>(null);
  const [activeSessionId, setActiveSessionId] = useState<string | null>(null);
  const [loading, setLoading] = useState(client !== null);
  const [error, setError] = useState<Error | null>(null);

  const refresh = useCallback(async () => {
    if (client === null) return;
    setLoading(true);
    try {
      const [records, serviceConfig] = await Promise.all([
        client.get<SessionRecord[]>("/api/sessions"),
        client.get<ServiceConfig>("/api/config"),
      ]);
      const visible = unarchived(records);
      setSessions(visible);
      setConfig(serviceConfig);
      setActiveSessionId((current) => {
        if (current !== null && visible.some((record) => record.session_id === current)) {
          return current;
        }
        return visible[0]?.session_id ?? null;
      });
      setError(null);
    } catch (value) {
      setError(errorFrom(value));
    } finally {
      setLoading(false);
    }
  }, [client]);

  useEffect(() => {
    if (client === null) {
      setLoading(false);
      return;
    }
    void refresh();
  }, [client, refresh]);

  const createSession = useCallback(async () => {
    if (client === null || config === null) return;
    try {
      const created = await client.post<SessionRecord>("/api/sessions", {
        workspace: config.base_workspace,
        mode: config.default_mode,
        context_mode: config.default_context_mode,
        title: "New chat",
      });
      setSessions((current) => [created, ...current.filter((item) => item.session_id !== created.session_id)]);
      setActiveSessionId(created.session_id);
      setError(null);
    } catch (value) {
      setError(errorFrom(value));
    }
  }, [client, config]);

  const selectSession = useCallback(
    (sessionId: string) => {
      if (sessions.some((record) => record.session_id === sessionId)) {
        setActiveSessionId(sessionId);
      }
    },
    [sessions],
  );

  const renameSession = useCallback(
    async (sessionId: string, title: string) => {
      if (client === null) return;
      try {
        const renamed = await client.patch<SessionRecord>(
          `/api/sessions/${encodeURIComponent(sessionId)}`,
          { title },
        );
        setSessions((current) =>
          current.map((record) => (record.session_id === sessionId ? renamed : record)),
        );
        setError(null);
      } catch (value) {
        setError(errorFrom(value));
      }
    },
    [client],
  );

  const archiveSession = useCallback(
    async (sessionId: string) => {
      if (client === null) return;
      try {
        await client.delete(`/api/sessions/${encodeURIComponent(sessionId)}`);
        setSessions((current) => {
          const remaining = current.filter((record) => record.session_id !== sessionId);
          setActiveSessionId((active) =>
            active === sessionId ? (remaining[0]?.session_id ?? null) : active,
          );
          return remaining;
        });
        setError(null);
      } catch (value) {
        setError(errorFrom(value));
      }
    },
    [client],
  );

  return {
    sessions,
    activeSessionId,
    loading,
    error,
    createSession,
    selectSession,
    renameSession,
    archiveSession,
    refresh,
  };
}
