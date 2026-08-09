import { useEffect, useMemo, useRef, useState } from "react";

import { ApiClient } from "./api/http";
import { useSessionSockets } from "./api/useSessionSockets";
import { CommandPalette } from "./components/CommandPalette";
import { ConnectionStatus as ServiceStatus } from "./components/ConnectionStatus";
import { RecoveryView } from "./components/RecoveryView";
import { Composer } from "./features/conversation/Composer";
import type { DraftStorage } from "./features/conversation/useDraft";
import { Conversation } from "./features/conversation/Conversation";
import type { OptimisticUserMessage } from "./features/conversation/Conversation";
import { Onboarding } from "./features/onboarding/Onboarding";
import { ActivityInspector } from "./features/inspector/ActivityInspector";
import { useInspector } from "./features/inspector/useInspector";
import type { InspectorStorage } from "./features/inspector/useInspector";
import type { CopyText } from "./features/conversation/CodeBlock";
import { ConversationHeader } from "./features/sessions/ConversationHeader";
import { SessionSidebar } from "./features/sessions/SessionSidebar";
import { SessionOperationRecovery } from "./features/sessions/SessionOperationRecovery";
import type { ConnectionStatus } from "./features/sessions/SessionSidebar";
import type { ServiceHealth, SessionRuntimeState } from "./features/sessions/useSessions";
import { useSessions } from "./features/sessions/useSessions";
import { NotificationObserver } from "./features/settings/NotificationObserver";
import { Settings } from "./features/settings/Settings";
import type { PreferenceStorage } from "./features/settings/preferences";
import { usePreferences } from "./features/settings/preferences";
import type { PlatformAdapter, ServiceConnection } from "./platform/types";
import { emptyTranscript } from "./protocol/reducer";
import type { ClientEvent, SendMessage, TranscriptState, TranscriptTerminal } from "./protocol/types";

const emptyTranscriptState = emptyTranscript();

type SubmissionCandidate = {
  readonly submissionId: string;
  readonly client: ApiClient;
  readonly dispatcher: AppProps["onSessionEvent"];
  readonly generation: number;
  readonly messageCountAtDispatch: number;
  readonly event: SendMessage;
};

type BoundSubmission = SubmissionCandidate & {
  readonly turnId: string;
};

type RetryAuthority = {
  readonly key: string;
  readonly sessionId: string;
  readonly client: ApiClient;
  readonly dispatcher: AppProps["onSessionEvent"];
  readonly terminal: TranscriptTerminal;
  readonly event: SendMessage;
};

type BootstrapLifecycle = {
  readonly status: "checking" | "reconnecting";
  readonly attempt: number;
  readonly retrying: boolean;
};

function terminalKey(sessionId: string, terminal: TranscriptTerminal): string {
  return `${sessionId}:${terminal.generation}:${terminal.sequence}:${terminal.turnId}`;
}

function newSubmissionId(): string {
  return globalThis.crypto.randomUUID();
}

function userMessageText(message: TranscriptState["messages"][number]): string | null {
  if (message.role !== "user") return null;
  if (typeof message.content === "string") return message.content;
  if (!Array.isArray(message.content)) return null;
  const parts = message.content.flatMap((part) => {
    if (typeof part === "string") return [part];
    if (part === null || Array.isArray(part) || typeof part !== "object") return [];
    return typeof part.text === "string" ? [part.text] : [];
  });
  return parts.length === 0 ? null : parts.join("\n");
}

function hasAuthoritativePrompt(candidate: SubmissionCandidate, transcript: TranscriptState): boolean {
  if (transcript.messages.length <= candidate.messageCountAtDispatch) return false;
  return transcript.messages
    .slice(candidate.messageCountAtDispatch)
    .some((message) => userMessageText(message) === candidate.event.text);
}

type AppProps = {
  readonly client?: ApiClient;
  readonly runtimeBySession?: Readonly<Record<string, SessionRuntimeState>>;
  readonly transcriptBySession?: Readonly<Record<string, TranscriptState>>;
  readonly branchBySession?: Readonly<Record<string, string | null>>;
  readonly onSessionEvent?: (sessionId: string, event: ClientEvent) => void | Promise<void>;
  readonly onStopSession?: (sessionId: string) => void | Promise<void>;
  readonly onOpenSettings?: () => void;
  readonly onToggleActivity?: () => void;
  readonly platformAdapter?: PlatformAdapter;
  readonly preferenceStorage?: PreferenceStorage;
  readonly sidebarStorage?: Pick<Storage, "getItem" | "setItem">;
  readonly draftStorage?: DraftStorage;
  readonly inspectorStorage?: InspectorStorage;
  readonly inspectorCopyText?: CopyText;
};

export function App({
  client: providedClient,
  runtimeBySession: providedRuntimeBySession,
  transcriptBySession: providedTranscriptBySession,
  branchBySession = {},
  onSessionEvent: providedOnSessionEvent,
  onStopSession: providedOnStopSession,
  onOpenSettings = () => {},
  onToggleActivity = () => {},
  platformAdapter,
  preferenceStorage,
  sidebarStorage,
  draftStorage,
  inspectorStorage,
  inspectorCopyText,
}: AppProps) {
  const [connection, setConnection] = useState<{
    readonly client: ApiClient | null;
    readonly service: ServiceConnection | null;
    readonly status: ConnectionStatus;
    readonly platform: PlatformAdapter | null;
  }>(() =>
    providedClient === undefined
      ? { client: null, service: null, status: "connecting", platform: platformAdapter ?? null }
      : { client: providedClient, service: null, status: "connected", platform: platformAdapter ?? null },
  );
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [bootstrapAttempt, setBootstrapAttempt] = useState(0);
  const [bootstrapLifecycle, setBootstrapLifecycle] = useState<BootstrapLifecycle | null>(() =>
    providedClient === undefined
      ? { status: "checking", attempt: 0, retrying: true }
      : null,
  );
  const [, setSubmissionRevision] = useState(0);
  const composerDraftMemory = useRef(new Map<string, string>());
  const pendingSubmissionRef = useRef(new Map<string, Map<string, SubmissionCandidate>>());
  const queuedSubmissionRef = useRef(new Map<string, string>());
  const boundSubmissionRef = useRef(new Map<string, BoundSubmission>());
  const optimisticSubmissionRef = useRef(new Map<string, SubmissionCandidate>());
  const submissionOwnerRef = useRef<{ readonly client: ApiClient | null; readonly dispatcher: AppProps["onSessionEvent"] } | null>(null);
  const retrySubmittedRef = useRef(new Set<string>());
  const bootstrapPendingRef = useRef(providedClient === undefined);
  const preferenceModel = usePreferences(preferenceStorage ?? sidebarStorage);

  useEffect(() => {
    if (providedClient !== undefined) {
      bootstrapPendingRef.current = false;
      setBootstrapLifecycle(null);
      setConnection({ client: providedClient, service: null, status: "connected", platform: platformAdapter ?? null });
      return;
    }
    let current = true;
    let currentPlatform = platformAdapter ?? null;
    bootstrapPendingRef.current = true;
    setBootstrapLifecycle({ status: "checking", attempt: bootstrapAttempt, retrying: true });
    setConnection({ client: null, service: null, status: "connecting", platform: platformAdapter ?? null });
    const loadPlatform = platformAdapter === undefined
      ? import("./platform").then(({ platform }) => platform)
      : Promise.resolve(platformAdapter);
    void loadPlatform
      .then(async (platform) => {
        currentPlatform = platform;
        const service = await platform.getServiceConnection();
        const client = new ApiClient(service);
        await client.get<ServiceHealth>("/api/health");
        return { platform, client, service };
      })
      .then(({ platform, client, service }) => {
        if (current) {
          bootstrapPendingRef.current = false;
          setBootstrapLifecycle(null);
          setConnection({ client, service, status: "connected", platform });
        }
      })
      .catch(() => {
        if (current) {
          bootstrapPendingRef.current = false;
          setBootstrapLifecycle({
            status: "reconnecting",
            attempt: bootstrapAttempt,
            retrying: false,
          });
          setConnection({ client: null, service: null, status: "connecting", platform: currentPlatform });
        }
      });
    return () => {
      current = false;
    };
  }, [bootstrapAttempt, platformAdapter, providedClient]);

  const retryBootstrap = () => {
    if (providedClient !== undefined || bootstrapPendingRef.current) return;
    bootstrapPendingRef.current = true;
    setBootstrapAttempt((attempt) => attempt + 1);
  };

  const sessionsModel = useSessions(connection.client, sidebarStorage);
  const sessionIds = useMemo(
    () => sessionsModel.sessions.map((session) => session.session_id),
    [sessionsModel.sessions],
  );
  const socketModel = useSessionSockets(connection.service, sessionIds);
  const runtimeBySession = providedRuntimeBySession ?? socketModel.runtimeBySession;
  const transcriptBySession = providedTranscriptBySession ?? socketModel.transcriptBySession;
  const onSessionEvent = providedOnSessionEvent ?? socketModel.send;
  const onStopSession = providedOnStopSession ?? socketModel.stop;
  const activeSession = useMemo(
    () =>
      sessionsModel.sessions.find(
        (session) => session.session_id === sessionsModel.activeSessionId,
      ) ?? null,
    [sessionsModel.activeSessionId, sessionsModel.sessions],
  );
  const activeTranscript = activeSession === null
    ? null
    : transcriptBySession[activeSession.session_id] ?? emptyTranscriptState;
  const latestRetryContextRef = useRef({
    client: connection.client,
    activeSessionId: activeSession?.session_id ?? null,
    transcriptBySession,
    dispatcher: onSessionEvent,
  });
  latestRetryContextRef.current = {
    client: connection.client,
    activeSessionId: activeSession?.session_id ?? null,
    transcriptBySession,
    dispatcher: onSessionEvent,
  };
  const retireCandidate = (sessionId: string, candidate: SubmissionCandidate) => {
    const pending = pendingSubmissionRef.current.get(sessionId);
    if (pending === undefined) return false;
    if (pending.get(candidate.submissionId) !== candidate) return false;
    pending.delete(candidate.submissionId);
    if (pending.size === 0) pendingSubmissionRef.current.delete(sessionId);
    return true;
  };
  const routeSessionEvent = (sessionId: string, clientEvent: ClientEvent) => {
    let candidate: SubmissionCandidate | undefined;
    let previousQueuedId: string | undefined;
    let clearingCandidateId: string | undefined;
    let outboundEvent = clientEvent;
    if (
      (clientEvent.type === "send_message" || clientEvent.type === "queue_message")
      && connection.client !== null
    ) {
      const transcript = transcriptBySession[sessionId] ?? emptyTranscriptState;
      const submissionId = newSubmissionId();
      outboundEvent = { ...clientEvent, submission_id: submissionId };
      candidate = {
        submissionId,
        client: connection.client,
        dispatcher: onSessionEvent,
        generation: transcript.generation,
        messageCountAtDispatch: transcript.messages.length,
        event: {
          type: "send_message",
          text: clientEvent.text,
          mode: clientEvent.mode,
          submission_id: submissionId,
        },
      };
      const pending = pendingSubmissionRef.current.get(sessionId) ?? new Map();
      pending.set(submissionId, candidate);
      pendingSubmissionRef.current.set(sessionId, pending);
      if (clientEvent.type === "queue_message") {
        previousQueuedId = queuedSubmissionRef.current.get(sessionId);
        queuedSubmissionRef.current.set(sessionId, submissionId);
      } else {
        optimisticSubmissionRef.current.set(sessionId, candidate);
        setSubmissionRevision((revision) => revision + 1);
      }
    } else if (clientEvent.type === "clear_queued_message") {
      clearingCandidateId = queuedSubmissionRef.current.get(sessionId);
      if (clearingCandidateId !== undefined) queuedSubmissionRef.current.delete(sessionId);
    }
    const rollbackCandidate = () => {
      if (candidate !== undefined) {
        if (optimisticSubmissionRef.current.get(sessionId) === candidate) {
          optimisticSubmissionRef.current.delete(sessionId);
          setSubmissionRevision((revision) => revision + 1);
        }
        if (
          retireCandidate(sessionId, candidate)
          && queuedSubmissionRef.current.get(sessionId) === candidate.submissionId
        ) {
          if (previousQueuedId === undefined) queuedSubmissionRef.current.delete(sessionId);
          else queuedSubmissionRef.current.set(sessionId, previousQueuedId);
        }
        return;
      }
      if (clearingCandidateId === undefined) return;
      const pending = pendingSubmissionRef.current.get(sessionId);
      if (
        queuedSubmissionRef.current.has(sessionId)
        || pending?.has(clearingCandidateId) !== true
      ) return;
      queuedSubmissionRef.current.set(sessionId, clearingCandidateId);
    };
    try {
      const result = onSessionEvent(sessionId, outboundEvent);
      if (candidate === undefined && clearingCandidateId === undefined) return result;
      return Promise.resolve(result).catch((error: unknown) => {
        rollbackCandidate();
        throw error;
      });
    } catch (error) {
      rollbackCandidate();
      throw error;
    }
  };
  useEffect(() => {
    const owner = submissionOwnerRef.current;
    if (owner?.client !== connection.client || owner.dispatcher !== onSessionEvent) {
      pendingSubmissionRef.current.clear();
      queuedSubmissionRef.current.clear();
      boundSubmissionRef.current.clear();
      optimisticSubmissionRef.current.clear();
      setSubmissionRevision((revision) => revision + 1);
      submissionOwnerRef.current = { client: connection.client, dispatcher: onSessionEvent };
    }
    let changed = false;
    const owned = (candidate: SubmissionCandidate | undefined) =>
      candidate !== undefined
      && candidate.client === connection.client
      && candidate.dispatcher === onSessionEvent;
    for (const [sessionId, transcript] of Object.entries(transcriptBySession)) {
      const pending = pendingSubmissionRef.current.get(sessionId);
      const previousBound = boundSubmissionRef.current.get(sessionId);
      const nextPending = new Map<string, SubmissionCandidate>();
      const queuedSubmissionId = transcript.queued?.submission_id ?? null;
      if (queuedSubmissionId !== null) {
        const queuedCandidate = pending?.get(queuedSubmissionId);
        if (owned(queuedCandidate)) {
          nextPending.set(queuedSubmissionId, {
            ...queuedCandidate!,
            generation: transcript.generation,
          });
          queuedSubmissionRef.current.set(sessionId, queuedSubmissionId);
        }
      } else {
        queuedSubmissionRef.current.delete(sessionId);
      }

      let nextBound: BoundSubmission | undefined;
      const activeTurn = transcript.activeTurn;
      if (activeTurn !== null && activeTurn.submissionId !== null) {
        const pendingActive = pending?.get(activeTurn.submissionId);
        if (owned(pendingActive)) {
          nextBound = {
            ...pendingActive!,
            generation: activeTurn.generation,
            turnId: activeTurn.turnId,
          };
        } else if (
          owned(previousBound)
          && previousBound?.submissionId === activeTurn.submissionId
          && previousBound.turnId === activeTurn.turnId
        ) {
          nextBound = { ...previousBound, generation: activeTurn.generation };
        }
      } else if (activeTurn === null) {
        const terminal = transcript.terminal;
        if (terminal?.submissionId !== null && terminal?.submissionId !== undefined) {
          for (const candidate of pending?.values() ?? []) {
            if (
              candidate.submissionId !== terminal.submissionId
              && owned(candidate)
              && candidate.generation === transcript.generation
            ) {
              nextPending.set(candidate.submissionId, candidate);
            }
          }
          const pendingTerminal = pending?.get(terminal.submissionId);
          if (terminal.kind === "failed" && owned(pendingTerminal) && pendingTerminal?.generation === terminal.generation) {
            nextBound = { ...pendingTerminal, turnId: terminal.turnId };
          } else if (
            terminal.kind === "failed"
            && owned(previousBound)
            && previousBound?.generation === terminal.generation
            && previousBound.submissionId === terminal.submissionId
            && previousBound.turnId === terminal.turnId
          ) {
            nextBound = previousBound;
          }
        } else if (terminal === null) {
          for (const candidate of pending?.values() ?? []) {
            if (owned(candidate) && candidate.generation === transcript.generation) {
              nextPending.set(candidate.submissionId, candidate);
            }
          }
        }
      }

      if (nextPending.size === 0) pendingSubmissionRef.current.delete(sessionId);
      else pendingSubmissionRef.current.set(sessionId, nextPending);
      if (nextBound === undefined) boundSubmissionRef.current.delete(sessionId);
      else boundSubmissionRef.current.set(sessionId, nextBound);
      changed = changed
        || pending !== pendingSubmissionRef.current.get(sessionId)
        || previousBound !== boundSubmissionRef.current.get(sessionId);

      const optimistic = optimisticSubmissionRef.current.get(sessionId);
      if (optimistic === undefined) continue;
      const settled = transcript.terminal?.submissionId === optimistic.submissionId
        && transcript.terminal.kind !== "failed";
      if (!owned(optimistic) || settled || hasAuthoritativePrompt(optimistic, transcript)) {
        optimisticSubmissionRef.current.delete(sessionId);
        changed = true;
        continue;
      }
      const remapped = nextBound?.submissionId === optimistic.submissionId
        ? nextBound
        : nextPending.get(optimistic.submissionId);
      if (optimistic.generation !== transcript.generation) {
        if (remapped === undefined) optimisticSubmissionRef.current.delete(sessionId);
        else optimisticSubmissionRef.current.set(sessionId, remapped);
        changed = true;
      }
    }
    if (changed) setSubmissionRevision((revision) => revision + 1);
  }, [connection.client, onSessionEvent, transcriptBySession]);
  const retryAuthority = (() => {
    if (
      connection.client === null
      || activeSession === null
      || activeTranscript === null
      || activeTranscript.error === null
      || activeTranscript.terminal?.kind !== "failed"
    ) return null;
    const retained = boundSubmissionRef.current.get(activeSession.session_id);
    if (
      retained === undefined
      || retained.client !== connection.client
      || retained.dispatcher !== onSessionEvent
      || retained.generation !== activeTranscript.terminal.generation
      || retained.turnId !== activeTranscript.terminal.turnId
    ) return null;
    const key = terminalKey(activeSession.session_id, activeTranscript.terminal);
    if (retrySubmittedRef.current.has(key)) return null;
    return {
      key,
      sessionId: activeSession.session_id,
      client: connection.client,
      dispatcher: onSessionEvent,
      terminal: activeTranscript.terminal,
      event: retained.event,
    } satisfies RetryAuthority;
  })();
  const retryFailedSubmission = retryAuthority === null ? undefined : async () => {
    const authority = retryAuthority;
    const latest = latestRetryContextRef.current;
    const transcript = latest.transcriptBySession[authority.sessionId];
    const currentTerminal = transcript?.terminal;
    const retained = boundSubmissionRef.current.get(authority.sessionId);
    if (
      latest.client !== authority.client
      || latest.dispatcher !== authority.dispatcher
      || latest.activeSessionId !== authority.sessionId
      || currentTerminal?.kind !== "failed"
      || terminalKey(authority.sessionId, currentTerminal) !== authority.key
      || retained?.client !== authority.client
      || retained.dispatcher !== authority.dispatcher
      || retained.generation !== authority.terminal.generation
      || retained.turnId !== authority.terminal.turnId
      || retrySubmittedRef.current.has(authority.key)
    ) return;
    const submissionId = newSubmissionId();
    const retryEvent: SendMessage = {
      ...authority.event,
      submission_id: submissionId,
    };
    const candidate: SubmissionCandidate = {
      submissionId,
      client: authority.client,
      dispatcher: authority.dispatcher,
      generation: authority.terminal.generation,
      messageCountAtDispatch: transcript?.messages.length ?? 0,
      event: retryEvent,
    };
    const pending = pendingSubmissionRef.current.get(authority.sessionId) ?? new Map();
    pending.set(submissionId, candidate);
    pendingSubmissionRef.current.set(authority.sessionId, pending);
    optimisticSubmissionRef.current.set(authority.sessionId, candidate);
    retrySubmittedRef.current.add(authority.key);
    try {
      await authority.dispatcher?.(authority.sessionId, retryEvent);
      setSubmissionRevision((revision) => revision + 1);
    } catch (error) {
      retireCandidate(authority.sessionId, candidate);
      if (optimisticSubmissionRef.current.get(authority.sessionId) === candidate) {
        optimisticSubmissionRef.current.delete(authority.sessionId);
      }
      retrySubmittedRef.current.delete(authority.key);
      setSubmissionRevision((revision) => revision + 1);
      throw error;
    }
  };
  const activeOptimisticCandidate = activeSession === null
    ? undefined
    : optimisticSubmissionRef.current.get(activeSession.session_id);
  const activeOptimisticSettled = activeOptimisticCandidate !== undefined
    && activeTranscript?.terminal?.submissionId === activeOptimisticCandidate.submissionId
    && activeTranscript.terminal.kind !== "failed";
  const optimisticUserMessages: readonly OptimisticUserMessage[] = activeOptimisticCandidate === undefined
    || activeTranscript === null
    || activeOptimisticSettled
    || hasAuthoritativePrompt(activeOptimisticCandidate, activeTranscript)
    ? []
    : [{
        submissionId: activeOptimisticCandidate.submissionId,
        content: activeOptimisticCandidate.event.text,
      }];
  const failedDraft = activeOptimisticCandidate !== undefined
    && activeTranscript?.terminal?.kind === "failed"
    && activeTranscript.terminal.submissionId === activeOptimisticCandidate.submissionId
    ? {
        submissionId: activeOptimisticCandidate.submissionId,
        text: activeOptimisticCandidate.event.text,
        mode: activeOptimisticCandidate.event.mode,
      }
    : null;
  const inspector = useInspector({
    sessionId: activeSession?.session_id ?? null,
    storage: inspectorStorage ?? sidebarStorage,
  });
  const activeElement = () => document.activeElement instanceof HTMLElement
    ? document.activeElement
    : null;
  const openInspectorOverview = () => {
    onToggleActivity();
    inspector.openOverview(activeElement());
  };
  const toggleInspector = () => {
    onToggleActivity();
    inspector.toggle(activeElement());
  };
  const openSettings = () => {
    onOpenSettings();
    setSettingsOpen(true);
  };
  const createFutureSession = async () => {
    const config = sessionsModel.config;
    if (config === null) return;
    await sessionsModel.createSession({
      workspace: config.base_workspace,
      mode: preferenceModel.preferences.defaultMode,
      contextMode: preferenceModel.preferences.contextMode,
      title: "New chat",
    });
  };
  const selectedPlatform = connection.platform ?? platformAdapter;
  const sessionReadiness = sessionsModel.refreshError !== null
    ? "reconnecting" as const
    : sessionsModel.loading ? "checking" as const : "connected" as const;
  const activeSocketReconnecting = activeSession !== null
    && socketModel.lifecycleBySession[activeSession.session_id] === "reconnecting";
  const showingFirstRun = connection.client !== null
    && sessionsModel.sessions.length === 0
    && (sessionsModel.loading || sessionsModel.config !== null);

  if (showingFirstRun && selectedPlatform !== undefined && selectedPlatform !== null) {
    return (
      <>
        <Onboarding
          serviceStatus={sessionsModel.loading ? "checking" : "ready"}
          platform={selectedPlatform}
          defaultWorkspace={sessionsModel.config?.base_workspace ?? ""}
          defaultMode={preferenceModel.preferences.defaultMode}
          defaultContextMode={preferenceModel.preferences.contextMode}
          onCreate={sessionsModel.createSession}
          onInvalidateCreate={sessionsModel.invalidateCreate}
          authorityKey={connection.client}
        />
        <NotificationObserver
          clientKey={connection.client}
          platform={selectedPlatform}
          sessions={sessionsModel.sessions}
          activeSessionId={sessionsModel.activeSessionId}
          transcriptBySession={transcriptBySession}
          preferences={preferenceModel.preferences}
        />
      </>
    );
  }

  return (
    <div className="app-shell">
      <SessionSidebar
        sessions={sessionsModel.sessions}
        activeSessionId={sessionsModel.activeSessionId}
        runtimeBySession={runtimeBySession}
        connectionStatus={connection.client !== null && sessionReadiness !== "connected"
          ? "connecting"
          : connection.status}
        storage={sidebarStorage}
        collapsed={preferenceModel.preferences.sidebarCollapsed}
        onCollapsedChange={(sidebarCollapsed) => preferenceModel.update({ sidebarCollapsed })}
        onCreate={createFutureSession}
        onSelect={sessionsModel.selectSession}
        onRename={sessionsModel.renameSession}
        onArchive={sessionsModel.archiveSession}
        onSearch={() => setPaletteOpen(true)}
        onOpenSettings={openSettings}
      />
      <div className="conversation-shell">
        {connection.client === null && bootstrapLifecycle !== null ? (
          <div className="connection-lifecycle">
            <ServiceStatus
              status={bootstrapLifecycle.status}
              attemptKey={`bootstrap:${bootstrapLifecycle.attempt}`}
              retrying={bootstrapLifecycle.retrying}
              onRetry={retryBootstrap}
            />
          </div>
        ) : activeSocketReconnecting && activeSession !== null ? (
          <div className="connection-lifecycle">
            <ServiceStatus
              status="reconnecting"
              attemptKey={activeSession.session_id}
              onRetry={() => socketModel.reconnect(activeSession.session_id)}
            />
          </div>
        ) : connection.client === null || sessionReadiness === "connected" ? null : (
          <div className="connection-lifecycle">
            <ServiceStatus
              status={sessionReadiness}
              attemptKey={sessionsModel.refreshError ?? connection.client}
              retrying={sessionsModel.loading}
              onRetry={() => void sessionsModel.refresh()}
            />
          </div>
        )}
        {activeSession === null ? (
          <header className="conversation-header">
            <h1>Agent Harness</h1>
          </header>
        ) : (
          <ConversationHeader
            key={activeSession.session_id}
            sessionId={activeSession.session_id}
            workspace={activeSession.workspace}
            branch={branchBySession[activeSession.session_id] ?? null}
            mode={activeSession.mode}
            onSetSessionMode={(event) => routeSessionEvent(activeSession.session_id, event)}
            onToggleActivity={openInspectorOverview}
          />
        )}
        <main aria-label="Conversation" className="conversation-main">
          {sessionsModel.cleanupError !== null ? (
            <SessionOperationRecovery
              failure={sessionsModel.cleanupError}
              onRetry={sessionsModel.retryCleanup}
            />
          ) : sessionsModel.operationError !== null ? (
            <SessionOperationRecovery
              failure={sessionsModel.operationError}
              onRetry={sessionsModel.retryOperation}
            />
          ) : activeTranscript?.error !== null && activeTranscript?.error !== undefined && activeSession !== null && selectedPlatform !== null && selectedPlatform !== undefined ? (
            <RecoveryView
              key={activeTranscript.terminal === null
                ? `${activeSession.session_id}:recovery`
                : terminalKey(activeSession.session_id, activeTranscript.terminal)}
              error={{
                category: activeTranscript.error.category === "session_resume_error"
                  || activeTranscript.error.category === "session_resume_failure"
                  || activeTranscript.error.category === "missing_workspace"
                  || activeTranscript.error.category === "invalid_workspace"
                  ? activeTranscript.error.category
                  : "turn_failure",
                message: activeTranscript.error.message,
              }}
              sessionId={activeSession.session_id}
              platform={selectedPlatform}
              onArchive={sessionsModel.archiveSession}
              onRetry={retryFailedSubmission}
            />
          ) : activeTranscript === null ? null : (
            <Conversation
              key={`conversation-${activeSession?.session_id}`}
              state={activeTranscript}
              optimisticUserMessages={optimisticUserMessages}
              openInspector={(activityId) => {
                onToggleActivity();
                inspector.openActivity(activityId, activeElement());
              }}
              ownsSearchShortcut
              onSessionEvent={(event) => activeSession === null
                ? undefined
                : routeSessionEvent(activeSession.session_id, event)}
            />
          )}
        </main>
        {activeSession !== null && activeTranscript !== null ? (
          <Composer
            sessionId={activeSession.session_id}
            running={activeTranscript.running}
            stopping={activeTranscript.stopping}
            queued={activeTranscript.queued}
            onEvent={(event) => routeSessionEvent(activeSession.session_id, event)}
            onStop={() => onStopSession(activeSession.session_id)}
            draftStorage={draftStorage}
            draftMemory={composerDraftMemory.current}
            failedDraft={failedDraft}
          />
        ) : null}
      </div>
      {activeSession !== null && activeTranscript !== null ? (
        <ActivityInspector
          open={inspector.open}
          narrow={inspector.narrow}
          width={inspector.width}
          pinned={inspector.pinned}
          sessionId={activeSession.session_id}
          contextMode={activeSession.context_mode}
          state={activeTranscript}
          selectedActivityId={inspector.selectedActivityId}
          onClose={inspector.close}
          onPinnedChange={inspector.setPinned}
          onSelectActivity={inspector.selectActivity}
          onWidthChange={inspector.setWidth}
          copyText={inspectorCopyText}
        />
      ) : null}
      <CommandPalette
        open={paletteOpen}
        onOpenChange={setPaletteOpen}
        sessions={sessionsModel.sessions}
        onNewChat={createFutureSession}
        onOpenSettings={openSettings}
        onToggleActivity={toggleInspector}
        onSelectSession={sessionsModel.selectSession}
      />
      <Settings
        open={settingsOpen}
        onOpenChange={setSettingsOpen}
        preferences={preferenceModel.preferences}
        onChange={preferenceModel.update}
        workspace={activeSession?.workspace ?? sessionsModel.config?.base_workspace ?? null}
        logsSupported={selectedPlatform?.openLogs !== undefined}
      />
      {selectedPlatform === null || selectedPlatform === undefined ? null : (
        <NotificationObserver
          clientKey={connection.client}
          platform={selectedPlatform}
          sessions={sessionsModel.sessions}
          activeSessionId={sessionsModel.activeSessionId}
          transcriptBySession={transcriptBySession}
          preferences={preferenceModel.preferences}
        />
      )}
    </div>
  );
}
