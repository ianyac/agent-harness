import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";

import { ApiError, type ApiClient } from "../../api/http";
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

export type SessionCleanupError = {
  readonly kind: "cleanup";
  readonly sessionId: string;
};

export type SessionCleanupStorage = Pick<Storage, "getItem" | "setItem">;

export type SessionsModel = {
  readonly sessions: SessionRecord[];
  readonly activeSessionId: string | null;
  readonly loading: boolean;
  readonly refreshError: Error | null;
  readonly operationError: SessionOperationError | null;
  readonly cleanupError: SessionCleanupError | null;
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
  readonly retryCleanup: () => Promise<void>;
  readonly refresh: () => Promise<void>;
};

export type ServiceHealth = {
  readonly status: "ok";
  readonly service_id: string;
};

type CleanupAuthority = {
  readonly client: ApiClient;
  readonly clientEpoch: number;
  readonly serviceId: string | null;
  readonly sessionId: string;
};

const CLEANUP_LEDGER_PREFIX = "agent-harness:superseded-session-cleanup:v1:";
const MAX_CLEANUP_RECORDS = 100;
const SERVICE_ID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

function defaultCleanupStorage(): SessionCleanupStorage | null {
  try {
    return typeof document === "undefined" || document.defaultView === null
      ? null
      : document.defaultView.localStorage;
  } catch {
    return null;
  }
}

function cleanupLedgerKey(serviceId: string): string {
  return `${CLEANUP_LEDGER_PREFIX}${serviceId}`;
}

function readCleanupLedger(
  storage: SessionCleanupStorage | null,
  serviceId: string,
): Set<string> {
  if (storage === null) return new Set();
  try {
    const raw = storage.getItem(cleanupLedgerKey(serviceId));
    if (raw === null) return new Set();
    const parsed = JSON.parse(raw) as unknown;
    if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) return new Set();
    const record = parsed as Record<string, unknown>;
    if (record.version !== 1 || !Array.isArray(record.sessionIds)) return new Set();
    return new Set(record.sessionIds
      .filter((value): value is string => (
        typeof value === "string" && value.length > 0 && value.length <= 512
      ))
      .slice(0, MAX_CLEANUP_RECORDS));
  } catch {
    return new Set();
  }
}

function writeCleanupLedger(
  storage: SessionCleanupStorage | null,
  serviceId: string,
  sessionIds: ReadonlySet<string>,
): void {
  if (storage === null) return;
  try {
    storage.setItem(cleanupLedgerKey(serviceId), JSON.stringify({
      version: 1,
      sessionIds: [...sessionIds].slice(0, MAX_CLEANUP_RECORDS),
    }));
  } catch {
    // The in-memory authority remains active and exposes manual recovery.
  }
}

function unarchived(records: readonly SessionRecord[]): SessionRecord[] {
  return records.filter((record) => record.archived_at === null);
}

function errorFrom(value: unknown): Error {
  return value instanceof Error ? value : new Error("The session operation failed.");
}

export function useSessions(
  client: ApiClient | null,
  providedCleanupStorage?: SessionCleanupStorage,
): SessionsModel {
  const [sessions, setSessions] = useState<SessionRecord[]>([]);
  const [config, setConfig] = useState<ServiceConfig | null>(null);
  const [activeSessionId, setActiveSessionId] = useState<string | null>(null);
  const [loading, setLoading] = useState(client !== null);
  const [refreshError, setRefreshError] = useState<Error | null>(null);
  const [operationError, setOperationError] = useState<SessionOperationError | null>(null);
  const [cleanupError, setCleanupError] = useState<SessionCleanupError | null>(null);
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
  const cleanupStorageRef = useRef<SessionCleanupStorage | null>(providedCleanupStorage ?? null);
  const serviceIdRef = useRef<string | null>(null);
  const cleanupLedgerRef = useRef(new Set<string>());
  const cleanupByServiceRef = useRef(new Map<string, Set<string>>());
  const cleanupAuthorityRef = useRef<CleanupAuthority | null>(null);
  const cleanupPendingRef = useRef(new Set<string>());

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

  const ledgerForService = useCallback((serviceId: string): Set<string> => {
    if (cleanupStorageRef.current === null) {
      cleanupStorageRef.current = defaultCleanupStorage();
    }
    const cached = cleanupByServiceRef.current.get(serviceId) ?? new Set<string>();
    const persisted = readCleanupLedger(cleanupStorageRef.current, serviceId);
    const merged = new Set([...cached, ...persisted]);
    cleanupByServiceRef.current.set(serviceId, merged);
    return merged;
  }, []);

  const activateCleanupLedger = useCallback((
    serviceId: string,
    requestClient: ApiClient,
    epoch: number,
  ): Set<string> => {
    const ledger = ledgerForService(serviceId);
    serviceIdRef.current = serviceId;
    cleanupLedgerRef.current = ledger;
    const first = ledger.values().next().value as string | undefined;
    cleanupAuthorityRef.current = first === undefined
      ? null
      : { client: requestClient, clientEpoch: epoch, serviceId, sessionId: first };
    setCleanupError(first === undefined ? null : { kind: "cleanup", sessionId: first });
    return ledger;
  }, [ledgerForService]);

  const retainCleanup = useCallback((authority: CleanupAuthority) => {
    let ledger: Set<string>;
    if (authority.serviceId === null) {
      ledger = cleanupLedgerRef.current;
    } else {
      ledger = ledgerForService(authority.serviceId);
    }
    ledger.add(authority.sessionId);
    if (authority.serviceId !== null) {
      writeCleanupLedger(cleanupStorageRef.current, authority.serviceId, ledger);
    }
    if (
      clientRef.current === authority.client
      && clientEpochRef.current === authority.clientEpoch
      && serviceIdRef.current === authority.serviceId
    ) {
      cleanupLedgerRef.current = ledger;
      cleanupAuthorityRef.current = authority;
      setCleanupError({ kind: "cleanup", sessionId: authority.sessionId });
    }
  }, [ledgerForService]);

  const confirmCleanup = useCallback((authority: CleanupAuthority) => {
    const ledger = authority.serviceId === null
      ? cleanupLedgerRef.current
      : ledgerForService(authority.serviceId);
    ledger.delete(authority.sessionId);
    if (authority.serviceId !== null) {
      writeCleanupLedger(cleanupStorageRef.current, authority.serviceId, ledger);
    }
    if (
      clientRef.current !== authority.client
      || clientEpochRef.current !== authority.clientEpoch
      || serviceIdRef.current !== authority.serviceId
    ) return;
    cleanupLedgerRef.current = ledger;
    const next = ledger.values().next().value as string | undefined;
    cleanupAuthorityRef.current = next === undefined
      ? null
      : { ...authority, sessionId: next };
    setCleanupError(next === undefined ? null : { kind: "cleanup", sessionId: next });
  }, [ledgerForService]);

  const refresh = useCallback(async () => {
    if (client === null) return;
    const requestClient = client;
    const epoch = clientEpochRef.current;
    const request = ++refreshRequestRef.current;
    const mutationVersion = mutationVersionRef.current;
    setLoading(true);
    try {
      const [records, serviceConfig, health] = await Promise.all([
        requestClient.get<SessionRecord[]>("/api/sessions"),
        requestClient.get<ServiceConfig>("/api/config"),
        requestClient.get<ServiceHealth>("/api/health").catch(() => null),
      ]);
      if (!isCurrentClient(requestClient, epoch) || refreshRequestRef.current !== request) return;
      const responseServiceId = health !== null
        && health.status === "ok"
        && typeof health.service_id === "string"
        && SERVICE_ID_PATTERN.test(health.service_id)
        ? health.service_id.toLowerCase()
        : null;
      if (responseServiceId === null) {
        throw new Error("The local service identity could not be confirmed.");
      }
      const cleanupLedger = activateCleanupLedger(responseServiceId, requestClient, epoch);
      const visible = unarchived(records).filter(
        (record) => !cleanupLedger.has(record.session_id),
      );
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
  }, [activateCleanupLedger, client, commitActiveSession, commitConfig, commitSessions, isCurrentClient]);

  useLayoutEffect(() => {
    cleanupStorageRef.current = providedCleanupStorage ?? null;
  }, [providedCleanupStorage]);

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
    serviceIdRef.current = null;
    cleanupLedgerRef.current = new Set();
    cleanupAuthorityRef.current = null;
    cleanupPendingRef.current.clear();
    setCleanupError(null);
  }, [client]);

  useEffect(() => {
    commitSessions([]);
    commitConfig(null);
    commitActiveSession(null);
    setRefreshError(null);
    commitOperationError(null);
    setCleanupError(null);
    if (client === null) {
      setLoading(false);
      return;
    }
    setLoading(true);
    void refresh();
  }, [client, commitActiveSession, commitConfig, commitOperationError, commitSessions, refresh]);

  const runCleanup = useCallback(async (authority: CleanupAuthority) => {
    const key = `${authority.clientEpoch}:${authority.serviceId ?? "ephemeral"}:${authority.sessionId}`;
    if (cleanupPendingRef.current.has(key)) return;
    cleanupPendingRef.current.add(key);
    retainCleanup(authority);
    try {
      await authority.client.delete(
        `/api/sessions/${encodeURIComponent(authority.sessionId)}`,
      );
      confirmCleanup(authority);
    } catch (value) {
      if (value instanceof ApiError && value.status === 404) {
        confirmCleanup(authority);
      } else {
        retainCleanup(authority);
      }
    } finally {
      cleanupPendingRef.current.delete(key);
    }
  }, [confirmCleanup, retainCleanup]);

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
    const requestServiceId = serviceIdRef.current;
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
      if (staleAuthority || !isCurrentClient(requestClient, epoch)) {
        await runCleanup({
          client: requestClient,
          clientEpoch: epoch,
          serviceId: requestServiceId,
          sessionId: created.session_id,
        });
        return { ok: false, error: new Error("The session request was superseded.") };
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
  }, [client, commitActiveSession, commitOperationError, commitSessions, isCurrentClient, runCleanup]);

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
        confirmCleanup({
          client: requestClient,
          clientEpoch: epoch,
          serviceId: serviceIdRef.current,
          sessionId,
        });
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
    [client, commitActiveSession, commitOperationError, commitSessions, confirmCleanup, isCurrentClient],
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

  const retryCleanup = useCallback(async () => {
    const authority = cleanupAuthorityRef.current;
    if (authority === null) return;
    if (
      clientRef.current !== authority.client
      || clientEpochRef.current !== authority.clientEpoch
      || serviceIdRef.current !== authority.serviceId
      || !cleanupLedgerRef.current.has(authority.sessionId)
    ) return;
    await runCleanup(authority);
  }, [runCleanup]);

  return {
    sessions,
    activeSessionId,
    loading,
    refreshError,
    operationError,
    cleanupError,
    config,
    createSession,
    invalidateCreate,
    selectSession,
    renameSession,
    archiveSession,
    retryOperation,
    retryCleanup,
    refresh,
  };
}
