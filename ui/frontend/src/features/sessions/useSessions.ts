import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";

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

export type ServiceConfig = {
  readonly base_workspace: string;
  readonly default_mode: BaseMode;
  readonly default_context_mode: ContextMode;
  readonly modes: BaseMode[];
  readonly context_modes: ContextMode[];
};

export type CreateSessionOptions = {
  readonly workspace: string;
  readonly mode: BaseMode;
  readonly contextMode: ContextMode;
  readonly title?: string;
};

export type CreateSessionAuthority = {
  readonly token: symbol;
};

export function createSessionAuthority(): CreateSessionAuthority {
  return { token: Symbol("create-session") };
}

export type CreateSessionResult =
  | { readonly ok: true; readonly session: SessionRecord }
  | { readonly ok: false; readonly error: Error };

export type SessionOperationError =
  | { readonly kind: "create"; readonly options: CreateSessionOptions; readonly error: Error }
  | { readonly kind: "rename"; readonly sessionId: string; readonly title: string; readonly error: Error }
  | { readonly kind: "archive"; readonly sessionId: string; readonly error: Error };

export type SessionsModel = {
  readonly sessions: SessionRecord[];
  readonly activeSessionId: string | null;
  readonly loading: boolean;
  readonly refreshError: Error | null;
  readonly operationError: SessionOperationError | null;
  readonly config: ServiceConfig | null;
  readonly createSession: (
    options?: CreateSessionOptions,
    authority?: CreateSessionAuthority,
  ) => Promise<CreateSessionResult>;
  readonly invalidateCreate: (authority: CreateSessionAuthority) => void;
  readonly selectSession: (sessionId: string) => void;
  readonly renameSession: (sessionId: string, title: string) => Promise<void>;
  readonly archiveSession: (sessionId: string) => Promise<void>;
  readonly retryOperation: () => Promise<void>;
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
  const [refreshError, setRefreshError] = useState<Error | null>(null);
  const [operationError, setOperationError] = useState<SessionOperationError | null>(null);
  const sessionsRef = useRef<SessionRecord[]>([]);
  const configRef = useRef<ServiceConfig | null>(null);
  const activeSessionIdRef = useRef<string | null>(null);
  const clientRef = useRef(client);
  const clientEpochRef = useRef(0);
  const refreshRequestRef = useRef(0);
  const mutationVersionRef = useRef(0);
  const sessionOperationRef = useRef(new Map<string, number>());
  const confirmedArchiveRef = useRef(new Set<string>());
  const confirmedRenameRef = useRef(
    new Map<string, { readonly operation: number; readonly record: SessionRecord }>(),
  );
  const selectionIntentRef = useRef(0);
  const operationErrorRef = useRef<SessionOperationError | null>(null);
  const operationErrorAuthorityRef = useRef(0);
  const currentCreateAuthorityRef = useRef<CreateSessionAuthority | null>(null);

  const commitSessions = useCallback((next: SessionRecord[]) => {
    sessionsRef.current = next;
    setSessions(next);
  }, []);

  const commitConfig = useCallback((next: ServiceConfig | null) => {
    configRef.current = next;
    setConfig(next);
  }, []);

  const commitActiveSession = useCallback((next: string | null) => {
    activeSessionIdRef.current = next;
    setActiveSessionId(next);
  }, []);

  const isCurrentClient = useCallback((requestClient: ApiClient, epoch: number) => {
    return clientRef.current === requestClient && clientEpochRef.current === epoch;
  }, []);

  const commitOperationError = useCallback((next: SessionOperationError | null) => {
    operationErrorRef.current = next;
    setOperationError(next);
  }, []);

  const refresh = useCallback(async () => {
    if (client === null) return;
    const requestClient = client;
    const epoch = clientEpochRef.current;
    const request = ++refreshRequestRef.current;
    const mutationVersion = mutationVersionRef.current;
    setLoading(true);
    try {
      const [records, serviceConfig] = await Promise.all([
        requestClient.get<SessionRecord[]>("/api/sessions"),
        requestClient.get<ServiceConfig>("/api/config"),
      ]);
      if (!isCurrentClient(requestClient, epoch) || refreshRequestRef.current !== request) return;
      const visible = unarchived(records);
      commitConfig(serviceConfig);
      if (mutationVersionRef.current === mutationVersion) {
        commitSessions(visible);
        const current = activeSessionIdRef.current;
        commitActiveSession(
          current !== null && visible.some((record) => record.session_id === current)
            ? current
            : (visible[0]?.session_id ?? null),
        );
      }
      setRefreshError(null);
    } catch (value) {
      if (isCurrentClient(requestClient, epoch) && refreshRequestRef.current === request) {
        setRefreshError(errorFrom(value));
      }
    } finally {
      if (isCurrentClient(requestClient, epoch) && refreshRequestRef.current === request) {
        setLoading(false);
      }
    }
  }, [client, commitActiveSession, commitConfig, commitSessions, isCurrentClient]);

  useLayoutEffect(() => {
    if (clientRef.current === client) return;
    clientRef.current = client;
    clientEpochRef.current += 1;
    refreshRequestRef.current += 1;
    mutationVersionRef.current += 1;
    sessionOperationRef.current.clear();
    confirmedArchiveRef.current.clear();
    confirmedRenameRef.current.clear();
    selectionIntentRef.current += 1;
    operationErrorAuthorityRef.current += 1;
    currentCreateAuthorityRef.current = null;
  }, [client]);

  useEffect(() => {
    commitSessions([]);
    commitConfig(null);
    commitActiveSession(null);
    setRefreshError(null);
    commitOperationError(null);
    if (client === null) {
      setLoading(false);
      return;
    }
    setLoading(true);
    void refresh();
  }, [client, commitActiveSession, commitConfig, commitOperationError, commitSessions, refresh]);

  const createSession = useCallback(async (
    options?: CreateSessionOptions,
    authority?: CreateSessionAuthority,
  ): Promise<CreateSessionResult> => {
    const serviceConfig = configRef.current;
    if (client === null || serviceConfig === null) {
      return { ok: false, error: new Error("The local service is not ready.") };
    }
    const request = options ?? {
      workspace: serviceConfig.base_workspace,
      mode: serviceConfig.default_mode,
      contextMode: serviceConfig.default_context_mode,
      title: "New chat",
    };
    const operationAuthority = ++operationErrorAuthorityRef.current;
    if (authority !== undefined) currentCreateAuthorityRef.current = authority;
    commitOperationError(null);
    if (
      !request.workspace.startsWith("/")
      || request.workspace.includes("\0")
      || request.workspace.trim() !== request.workspace
      || !serviceConfig.modes.includes(request.mode)
      || !serviceConfig.context_modes.includes(request.contextMode)
      || (request.title !== undefined && request.title.trim() === "")
    ) {
      const validationError = new Error("Invalid session creation options.");
      commitOperationError({ kind: "create", options: request, error: validationError });
      return { ok: false, error: validationError };
    }
    const requestClient = client;
    const epoch = clientEpochRef.current;
    const selectionIntent = ++selectionIntentRef.current;
    mutationVersionRef.current += 1;
    try {
      const created = await requestClient.post<SessionRecord>("/api/sessions", {
        workspace: request.workspace,
        mode: request.mode,
        context_mode: request.contextMode,
        title: request.title ?? "New chat",
      });
      const staleAuthority = authority !== undefined
        && currentCreateAuthorityRef.current !== authority;
      if (staleAuthority) {
        try {
          await requestClient.delete(`/api/sessions/${encodeURIComponent(created.session_id)}`);
        } catch {
          // The stale session was never committed locally; cleanup failure cannot replace newer state.
        }
        return { ok: false, error: new Error("The session request was superseded.") };
      }
      if (!isCurrentClient(requestClient, epoch)) {
        return { ok: false, error: new Error("The session client was replaced.") };
      }
      mutationVersionRef.current += 1;
      commitSessions([
        created,
        ...sessionsRef.current.filter((item) => item.session_id !== created.session_id),
      ]);
      if (selectionIntentRef.current === selectionIntent) {
        commitActiveSession(created.session_id);
      }
      if (authority !== undefined && currentCreateAuthorityRef.current === authority) {
        currentCreateAuthorityRef.current = null;
      }
      if (operationErrorAuthorityRef.current === operationAuthority) commitOperationError(null);
      return { ok: true, session: created };
    } catch (value) {
      const operationError = errorFrom(value);
      if (
        isCurrentClient(requestClient, epoch)
        && (authority === undefined || currentCreateAuthorityRef.current === authority)
        && operationErrorAuthorityRef.current === operationAuthority
      ) {
        commitOperationError({ kind: "create", options: request, error: operationError });
      }
      return { ok: false, error: operationError };
    }
  }, [client, commitActiveSession, commitOperationError, commitSessions, isCurrentClient]);

  const invalidateCreate = useCallback((authority: CreateSessionAuthority) => {
    if (currentCreateAuthorityRef.current !== authority) return;
    currentCreateAuthorityRef.current = null;
    operationErrorAuthorityRef.current += 1;
    if (operationErrorRef.current?.kind === "create") commitOperationError(null);
  }, [commitOperationError]);

  const selectSession = useCallback(
    (sessionId: string) => {
      if (sessionsRef.current.some((record) => record.session_id === sessionId)) {
        selectionIntentRef.current += 1;
        commitActiveSession(sessionId);
      }
    },
    [commitActiveSession],
  );

  const renameSession = useCallback(
    async (sessionId: string, title: string) => {
      if (client === null) return;
      const requestClient = client;
      const epoch = clientEpochRef.current;
      const errorAuthority = ++operationErrorAuthorityRef.current;
      commitOperationError(null);
      const operation = (sessionOperationRef.current.get(sessionId) ?? 0) + 1;
      sessionOperationRef.current.set(sessionId, operation);
      mutationVersionRef.current += 1;
      try {
        const renamed = await requestClient.patch<SessionRecord>(
          `/api/sessions/${encodeURIComponent(sessionId)}`,
          { title },
        );
        if (!isCurrentClient(requestClient, epoch)) return;
        mutationVersionRef.current += 1;
        if (confirmedArchiveRef.current.has(sessionId)) return;
        const confirmed = confirmedRenameRef.current.get(sessionId);
        if (confirmed === undefined || operation > confirmed.operation) {
          confirmedRenameRef.current.set(sessionId, { operation, record: renamed });
          commitSessions(
            sessionsRef.current.map((record) =>
              record.session_id === sessionId ? renamed : record,
            ),
          );
        }
        if (operationErrorAuthorityRef.current === errorAuthority) commitOperationError(null);
      } catch (value) {
        if (
          isCurrentClient(requestClient, epoch)
          && !confirmedArchiveRef.current.has(sessionId)
          && operationErrorAuthorityRef.current === errorAuthority
        ) {
          commitOperationError({ kind: "rename", sessionId, title, error: errorFrom(value) });
        }
      }
    },
    [client, commitOperationError, commitSessions, isCurrentClient],
  );

  const archiveSession = useCallback(
    async (sessionId: string) => {
      if (client === null) return;
      const requestClient = client;
      const epoch = clientEpochRef.current;
      const errorAuthority = ++operationErrorAuthorityRef.current;
      commitOperationError(null);
      const operation = (sessionOperationRef.current.get(sessionId) ?? 0) + 1;
      sessionOperationRef.current.set(sessionId, operation);
      mutationVersionRef.current += 1;
      try {
        await requestClient.delete(`/api/sessions/${encodeURIComponent(sessionId)}`);
        if (!isCurrentClient(requestClient, epoch)) return;
        mutationVersionRef.current += 1;
        confirmedArchiveRef.current.add(sessionId);
        confirmedRenameRef.current.delete(sessionId);
        const remaining = sessionsRef.current.filter((record) => record.session_id !== sessionId);
        commitSessions(remaining);
        if (activeSessionIdRef.current === sessionId) {
          commitActiveSession(remaining[0]?.session_id ?? null);
        }
        if (operationErrorAuthorityRef.current === errorAuthority) commitOperationError(null);
      } catch (value) {
        if (
          isCurrentClient(requestClient, epoch)
          && !confirmedArchiveRef.current.has(sessionId)
          && operationErrorAuthorityRef.current === errorAuthority
        ) {
          commitOperationError({ kind: "archive", sessionId, error: errorFrom(value) });
        }
      }
    },
    [client, commitActiveSession, commitOperationError, commitSessions, isCurrentClient],
  );

  const retryOperation = useCallback(async () => {
    const failed = operationErrorRef.current;
    if (failed === null) return;
    if (failed.kind === "create") {
      await createSession(failed.options);
      return;
    }
    if (failed.kind === "rename") {
      await renameSession(failed.sessionId, failed.title);
      return;
    }
    await archiveSession(failed.sessionId);
  }, [archiveSession, createSession, renameSession]);

  return {
    sessions,
    activeSessionId,
    loading,
    refreshError,
    operationError,
    config,
    createSession,
    invalidateCreate,
    selectSession,
    renameSession,
    archiveSession,
    retryOperation,
    refresh,
  };
}
