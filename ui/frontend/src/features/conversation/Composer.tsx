import { ArrowUp, Paperclip, Pencil, RotateCcw, Square, X } from "lucide-react";
import {
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
} from "react";

import type { ClientEvent, QueuedMessage, TurnMode } from "../../protocol/types";
import styles from "./composer.module.css";
import { useDraft } from "./useDraft";
import type { DraftStorage } from "./useDraft";

type ComposerProps = {
  readonly sessionId: string;
  readonly running: boolean;
  readonly stopping: boolean;
  readonly queued: QueuedMessage | null;
  readonly onEvent: (event: ClientEvent) => void | Promise<void>;
  readonly onStop: () => void | Promise<void>;
  readonly draftStorage?: DraftStorage;
  readonly backupDelayMs?: number;
  readonly draftMemory?: Map<string, string>;
  readonly failedDraft?: {
    readonly submissionId: string;
    readonly text: string;
    readonly mode: TurnMode;
  } | null;
};

type Submission = Extract<ClientEvent, { type: "send_message" | "queue_message" }>;

type EditAuthority = {
  readonly sessionId: string;
  readonly sessionEpoch: number;
  readonly editRevision: number;
};

type ClearOperation = EditAuthority & {
  readonly operationId: number;
  readonly queued: QueuedMessage;
  readonly restoreToDraft: boolean;
};

type QueueReconciliation = {
  readonly kind: "failed" | "saved";
  readonly operation: ClearOperation;
};

type DeliveryOperation = EditAuthority & {
  readonly operationId: number;
  readonly submission: Submission;
};

type StopOperation = {
  readonly sessionId: string;
  readonly operationId: number;
};

function hasSameEditAuthority(left: EditAuthority, right: EditAuthority) {
  return left.sessionId === right.sessionId &&
    left.sessionEpoch === right.sessionEpoch &&
    left.editRevision === right.editRevision;
}

function hasSameQueuedSlot(current: QueuedMessage | null, captured: QueuedMessage) {
  return current !== null && current.text === captured.text && current.mode === captured.mode;
}

function protectsFocus(element: Element | null): boolean {
  if (!(element instanceof HTMLElement)) return false;
  if (element.closest('[role="search"]') !== null) return true;
  if (element.getAttribute("role") === "searchbox") return true;
  const editable = element.closest("[contenteditable]");
  if (editable !== null && editable.getAttribute("contenteditable") !== "false") return true;
  if (element instanceof HTMLTextAreaElement) return true;
  return element instanceof HTMLInputElement &&
    !["button", "checkbox", "color", "file", "image", "radio", "range", "reset", "submit"]
      .includes(element.type);
}

const commands: ReadonlyArray<{
  readonly command: string;
  readonly label: string;
  readonly description: string;
  readonly mode: TurnMode;
}> = [
  {
    command: "/plan",
    label: "Plan mode",
    description: "Plan this request before making changes",
    mode: "plan",
  },
  {
    command: "/base",
    label: "Base mode",
    description: "Use the session's base permission mode",
    mode: "base",
  },
];

export function Composer({
  sessionId,
  running,
  stopping,
  queued,
  onEvent,
  onStop,
  draftStorage,
  backupDelayMs,
  draftMemory,
  failedDraft = null,
}: ComposerProps) {
  const [draft, setDraft, setSessionDraft] = useDraft(sessionId, {
    storage: draftStorage,
    backupDelayMs,
    memory: draftMemory,
  });
  const [modes, setModes] = useState<ReadonlyMap<string, TurnMode>>(() => new Map());
  const [suggestionsDismissed, setSuggestionsDismissed] = useState(false);
  const [activeSuggestion, setActiveSuggestion] = useState(0);
  const [deliveryErrors, setDeliveryErrors] = useState<
    ReadonlyMap<string, DeliveryOperation>
  >(() => new Map());
  const [turnErrorSessions, setTurnErrorSessions] = useState<ReadonlySet<string>>(
    () => new Set(),
  );
  const [clearPendingSessions, setClearPendingSessions] = useState<ReadonlySet<string>>(
    () => new Set(),
  );
  const [queueReconciliations, setQueueReconciliations] = useState<
    ReadonlyMap<string, QueueReconciliation>
  >(() => new Map());
  const composing = useRef(false);
  // IME commit orders differ: Chrome fires the committing Enter keydown with
  // isComposing before compositionend; Safari fires compositionend first and
  // the committing Enter arrives after with isComposing already false.
  const composedEnter = useRef(false);
  const pendingCommitEnter = useRef(false);
  const editAuthorities = useRef(new Map<string, EditAuthority>());
  const nextSessionEpoch = useRef(0);
  const nextDeliveryOperationId = useRef(0);
  const currentDeliveryOperations = useRef(new Map<string, DeliveryOperation>());
  const nextClearOperationId = useRef(0);
  const currentClearOperations = useRef(new Map<string, ClearOperation>());
  const nextStopOperationId = useRef(0);
  const currentStopOperations = useRef(new Map<string, StopOperation>());
  const handledFailedDrafts = useRef(new Set<string>());
  const activeSessionRef = useRef(sessionId);
  activeSessionRef.current = sessionId;
  const composerRef = useRef<HTMLElement>(null);
  const textboxRef = useRef<HTMLTextAreaElement>(null);
  const stopRef = useRef<HTMLButtonElement>(null);
  const runningBySession = useRef(new Map<string, boolean>([[sessionId, running]]));
  const previousActiveSession = useRef(sessionId);
  const listboxId = useId();
  const slashQuery = draft.startsWith("/") ? draft.split(/\s/, 1)[0].toLocaleLowerCase() : "";
  const matchingCommands = useMemo(
    () => slashQuery === "" ? [] : commands.filter(({ command }) => command.startsWith(slashQuery)),
    [slashQuery],
  );
  const suggestionsOpen = !suggestionsDismissed && matchingCommands.length > 0;
  const blank = draft.trim() === "";
  const mode = modes.get(sessionId) ?? "base";
  const deliveryError = deliveryErrors.get(sessionId) ?? null;
  const queueReconciliation = queueReconciliations.get(sessionId) ?? null;
  const clearPending = clearPendingSessions.has(sessionId);
  const turnError = turnErrorSessions.has(sessionId);

  const editAuthorityFor = (authoritySessionId: string) => {
    const existing = editAuthorities.current.get(authoritySessionId);
    if (existing !== undefined) return existing;
    nextSessionEpoch.current += 1;
    const created: EditAuthority = {
      sessionId: authoritySessionId,
      sessionEpoch: nextSessionEpoch.current,
      editRevision: 0,
    };
    editAuthorities.current.set(authoritySessionId, created);
    return created;
  };
  editAuthorityFor(sessionId);

  useEffect(() => {
    setSuggestionsDismissed(false);
    setActiveSuggestion(0);
    composing.current = false;
    composedEnter.current = false;
    pendingCommitEnter.current = false;
  }, [sessionId]);

  useEffect(() => {
    const sessionRemainedActive = previousActiveSession.current === sessionId;
    const previousRunning = runningBySession.current.get(sessionId);
    runningBySession.current.set(sessionId, running);
    previousActiveSession.current = sessionId;
    if (sessionRemainedActive && previousRunning === true && !running) {
      const active = document.activeElement;
      const insideComposer = composerRef.current?.contains(active) ?? false;
      if (insideComposer || !protectsFocus(active)) textboxRef.current?.focus();
    }
  }, [running, sessionId]);

  // Escape-dismissal of suggestions holds until the slash query itself
  // changes, not on every keystroke after it.
  useEffect(() => {
    setActiveSuggestion(0);
    setSuggestionsDismissed(false);
  }, [slashQuery]);

  // Escape focuses Stop from anywhere once transient UI is out of the way.
  useEffect(() => {
    const onWindowKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape" || event.defaultPrevented) return;
      if (!running || suggestionsOpen) return;
      if (event.isComposing || event.keyCode === 229) return;
      if (document.querySelector('[role="dialog"], dialog[open], [role="search"]') !== null) return;
      const stop = stopRef.current;
      if (stop === null) return;
      event.preventDefault();
      stop.focus();
    };
    window.addEventListener("keydown", onWindowKeyDown);
    return () => window.removeEventListener("keydown", onWindowKeyDown);
  }, [running, suggestionsOpen]);

  const setDeliveryError = (
    errorSessionId: string,
    operation: DeliveryOperation | null,
  ) => {
    setDeliveryErrors((current) => {
      const next = new Map(current);
      if (operation === null) next.delete(errorSessionId);
      else next.set(errorSessionId, operation);
      return next;
    });
  };

  const deliver = async (submission: Submission, retryOperation?: DeliveryOperation) => {
    const origin = retryOperation ?? editAuthorityFor(sessionId);
    const active = currentDeliveryOperations.current.get(origin.sessionId);
    if (active !== undefined && hasSameEditAuthority(active, origin)) return;
    nextDeliveryOperationId.current += 1;
    const operation: DeliveryOperation = {
      sessionId: origin.sessionId,
      sessionEpoch: origin.sessionEpoch,
      editRevision: origin.editRevision,
      operationId: nextDeliveryOperationId.current,
      submission,
    };
    currentDeliveryOperations.current.set(operation.sessionId, operation);
    setDeliveryError(operation.sessionId, null);
    try {
      await onEvent(submission);
      if (currentDeliveryOperations.current.get(operation.sessionId) !== operation) return;
      currentDeliveryOperations.current.delete(operation.sessionId);
      if (hasSameEditAuthority(editAuthorityFor(operation.sessionId), operation)) {
        markEdited(operation.sessionId);
        setSessionDraft(operation.sessionId, "");
      }
    } catch {
      if (currentDeliveryOperations.current.get(operation.sessionId) !== operation) return;
      currentDeliveryOperations.current.delete(operation.sessionId);
      // Edit authority only decides draft clearing; a failure is always surfaced.
      setDeliveryError(operation.sessionId, operation);
    }
  };

  const submit = () => {
    if (blank) return;
    void deliver({
      type: running ? "queue_message" : "send_message",
      text: draft,
      mode,
    });
  };

  const markEdited = (editedSessionId = sessionId) => {
    setDeliveryError(editedSessionId, null);
    const current = editAuthorityFor(editedSessionId);
    editAuthorities.current.set(editedSessionId, {
      ...current,
      editRevision: current.editRevision + 1,
    });
  };

  const setSessionMode = (modeSessionId: string, next: TurnMode) => {
    setModes((current) => {
      if ((current.get(modeSessionId) ?? "base") === next) return current;
      const updated = new Map(current);
      updated.set(modeSessionId, next);
      return updated;
    });
  };

  // A pure mode change is not a draft edit and must not bump edit authority.
  const changeMode = (next: TurnMode) => {
    setSessionMode(sessionId, next);
  };

  useEffect(() => {
    if (failedDraft === null) return;
    const key = `${sessionId}:${failedDraft.submissionId}`;
    if (handledFailedDrafts.current.has(key)) return;
    handledFailedDrafts.current.add(key);
    if (draft !== "") return;
    markEdited(sessionId);
    setSessionDraft(sessionId, failedDraft.text);
    setSessionMode(sessionId, failedDraft.mode);
  }, [draft, failedDraft, sessionId, setSessionDraft]);

  const setQueueReconciliation = (
    reconciliationSessionId: string,
    reconciliation: QueueReconciliation | null,
  ) => {
    setQueueReconciliations((current) => {
      const next = new Map(current);
      if (reconciliation === null) next.delete(reconciliationSessionId);
      else next.set(reconciliationSessionId, reconciliation);
      return next;
    });
  };

  const setClearPending = (pendingSessionId: string, value: boolean) => {
    setClearPendingSessions((current) => {
      const next = new Set(current);
      if (value) next.add(pendingSessionId);
      else next.delete(pendingSessionId);
      return next;
    });
  };

  const setTurnError = (errorSessionId: string, value: boolean) => {
    setTurnErrorSessions((current) => {
      const next = new Set(current);
      if (value) next.add(errorSessionId);
      else next.delete(errorSessionId);
      return next;
    });
  };

  const clearQueue = async (restoreToDraft: boolean, retryOperation?: ClearOperation) => {
    if (
      retryOperation !== undefined &&
      (sessionId !== retryOperation.sessionId || !hasSameQueuedSlot(queued, retryOperation.queued))
    ) {
      setQueueReconciliation(retryOperation.sessionId, { kind: "saved", operation: retryOperation });
      return;
    }
    const queuedMessage = retryOperation?.queued ?? queued;
    const operationSessionId = retryOperation?.sessionId ?? sessionId;
    if (
      queuedMessage === null ||
      currentClearOperations.current.has(operationSessionId)
    ) return;
    const origin: EditAuthority = retryOperation ?? editAuthorityFor(sessionId);
    nextClearOperationId.current += 1;
    const operation: ClearOperation = {
      ...origin,
      operationId: nextClearOperationId.current,
      queued: queuedMessage,
      restoreToDraft: retryOperation?.restoreToDraft ?? restoreToDraft,
    };
    currentClearOperations.current.set(operation.sessionId, operation);
    setQueueReconciliation(operation.sessionId, null);
    setClearPending(operation.sessionId, true);
    try {
      await onEvent({ type: "clear_queued_message" });
      if (currentClearOperations.current.get(operation.sessionId) !== operation) return;
      currentClearOperations.current.delete(operation.sessionId);
      setClearPending(operation.sessionId, false);
      if (!operation.restoreToDraft) {
        if (activeSessionRef.current === operation.sessionId) textboxRef.current?.focus();
      } else if (hasSameEditAuthority(editAuthorityFor(operation.sessionId), operation)) {
        markEdited(operation.sessionId);
        setSessionDraft(operation.sessionId, operation.queued.text);
        setSessionMode(operation.sessionId, operation.queued.mode);
        if (activeSessionRef.current === operation.sessionId) textboxRef.current?.focus();
      } else {
        setQueueReconciliation(operation.sessionId, { kind: "saved", operation });
      }
    } catch {
      if (currentClearOperations.current.get(operation.sessionId) !== operation) return;
      currentClearOperations.current.delete(operation.sessionId);
      setClearPending(operation.sessionId, false);
      setQueueReconciliation(operation.sessionId, { kind: "failed", operation });
    }
  };

  const appendClearedFollowUp = (operation: ClearOperation) => {
    markEdited(operation.sessionId);
    setSessionDraft(operation.sessionId, (current) => current === ""
      ? operation.queued.text
      : `${current}\n${operation.queued.text}`);
    setQueueReconciliation(operation.sessionId, null);
    textboxRef.current?.focus();
  };

  const dismissClearedFollowUp = (operation: ClearOperation) => {
    setQueueReconciliation(operation.sessionId, null);
  };

  const chooseCommand = (index: number) => {
    const selected = matchingCommands[index];
    if (selected === undefined) return;
    markEdited();
    setSessionMode(sessionId, selected.mode);
    setDraft(draft.replace(/^\/\S+\s*/, ""));
    setSuggestionsDismissed(true);
    textboxRef.current?.focus();
  };

  const requestStop = async () => {
    nextStopOperationId.current += 1;
    const operation: StopOperation = {
      sessionId,
      operationId: nextStopOperationId.current,
    };
    currentStopOperations.current.set(operation.sessionId, operation);
    setTurnError(operation.sessionId, false);
    try {
      await onStop();
    } catch {
      if (currentStopOperations.current.get(operation.sessionId) !== operation) return;
      currentStopOperations.current.delete(operation.sessionId);
      setTurnError(operation.sessionId, true);
      return;
    }
    if (currentStopOperations.current.get(operation.sessionId) === operation) {
      currentStopOperations.current.delete(operation.sessionId);
    }
  };

  const queueActionName = queued === null ? "Queue message" : "Update queued message";

  return (
    <section
      ref={composerRef}
      className={styles.composerRegion}
      aria-label="Message composer"
      onKeyDown={(event) => {
        if (event.key !== "Escape" || !suggestionsOpen) return;
        event.preventDefault();
        setSuggestionsDismissed(true);
      }}
    >
      <div className={styles.composer}>
        {queued !== null ? (
          <div className={styles.queued} role="status" aria-label="Queued follow-up">
            <div className={styles.queuedCopy}>
              <span className={styles.queuedLabel}>Queued · {queued.mode === "plan" ? "Plan" : "Base"}</span>
              <span className={styles.queuedText}>{queued.text}</span>
            </div>
            <button
              type="button"
              aria-label="Edit queued follow-up"
              disabled={clearPending}
              onClick={() => void clearQueue(true)}
            >
              <Pencil aria-hidden="true" size={15} />
            </button>
            <button
              type="button"
              aria-label="Clear queued follow-up"
              disabled={clearPending}
              onClick={() => void clearQueue(false)}
            >
              <X aria-hidden="true" size={16} />
            </button>
          </div>
        ) : null}

        <div className={styles.editor}>
          {suggestionsOpen ? (
            <div className={styles.suggestions} role="listbox" aria-label="Composer commands" id={listboxId}>
              {matchingCommands.map((command, index) => (
                <button
                  key={command.command}
                  id={`${listboxId}-${index}`}
                  type="button"
                  role="option"
                  aria-selected={index === activeSuggestion}
                  onMouseDown={(event) => event.preventDefault()}
                  onClick={() => chooseCommand(index)}
                >
                  <span>{command.label}</span>
                  <span>{command.command} · {command.description}</span>
                </button>
              ))}
            </div>
          ) : null}
          <textarea
            ref={textboxRef}
            aria-label="Message"
            aria-keyshortcuts="Enter Meta+Enter"
            aria-autocomplete="list"
            aria-controls={suggestionsOpen ? listboxId : undefined}
            aria-activedescendant={suggestionsOpen ? `${listboxId}-${activeSuggestion}` : undefined}
            rows={3}
            placeholder={running ? "Add a follow-up…" : "Message Agent Harness…"}
            value={draft}
            onChange={(event) => {
              markEdited();
              setDraft(event.target.value);
            }}
            onCompositionStart={() => {
              composing.current = true;
              composedEnter.current = false;
              pendingCommitEnter.current = false;
            }}
            onCompositionEnd={() => {
              composing.current = false;
              // Chrome's committing Enter keydown already passed; only Safari
              // still owes us one, which must be swallowed without submitting.
              pendingCommitEnter.current = !composedEnter.current;
              composedEnter.current = false;
            }}
            onKeyDown={(event) => {
              if (event.key !== "Enter") {
                pendingCommitEnter.current = false;
              } else {
                if (
                  event.nativeEvent.isComposing ||
                  event.nativeEvent.keyCode === 229 ||
                  composing.current
                ) {
                  composedEnter.current = true;
                  return;
                }
                if (pendingCommitEnter.current) {
                  pendingCommitEnter.current = false;
                  event.preventDefault();
                  return;
                }
              }
              if (suggestionsOpen && event.key === "ArrowDown") {
                event.preventDefault();
                setActiveSuggestion((index) => (index + 1) % matchingCommands.length);
                return;
              }
              if (suggestionsOpen && event.key === "ArrowUp") {
                event.preventDefault();
                setActiveSuggestion(
                  (index) => (index - 1 + matchingCommands.length) % matchingCommands.length,
                );
                return;
              }
              if (suggestionsOpen && event.key === "Enter" && !event.metaKey) {
                event.preventDefault();
                chooseCommand(activeSuggestion);
                return;
              }
              if (
                event.key === "Enter" &&
                !event.altKey &&
                !event.ctrlKey &&
                !event.shiftKey
              ) {
                event.preventDefault();
                submit();
              }
            }}
          />
        </div>

        <div className={styles.toolbar}>
          <button
            type="button"
            className={styles.iconButton}
            aria-label="Attach context (coming later)"
            disabled
          >
            <Paperclip aria-hidden="true" size={17} />
          </button>
          <div className={styles.modes} role="group" aria-label="Turn mode">
            <button
              type="button"
              aria-label="Base mode"
              aria-pressed={mode === "base"}
              onClick={() => changeMode("base")}
            >
              Base
            </button>
            <button
              type="button"
              aria-label="Plan mode"
              aria-pressed={mode === "plan"}
              onClick={() => changeMode("plan")}
            >
              Plan
            </button>
          </div>
          <span className={styles.spacer} />
          {running && !blank ? (
            <button
              type="button"
              className={styles.queueButton}
              aria-label={queueActionName}
              onClick={submit}
            >
              {queued === null ? "Queue" : "Update queue"}
            </button>
          ) : null}
          {running ? (
            <button
              ref={stopRef}
              type="button"
              className={styles.stopButton}
              style={{ aspectRatio: "1" }}
              aria-label={stopping ? "Stopping turn" : "Stop turn"}
              disabled={stopping}
              onClick={() => void requestStop()}
            >
              <Square aria-hidden="true" size={15} fill="currentColor" />
            </button>
          ) : (
            <button
              type="button"
              className={styles.sendButton}
              aria-label="Send message"
              disabled={blank}
              onClick={submit}
            >
              <ArrowUp aria-hidden="true" size={18} />
            </button>
          )}
        </div>

        {deliveryError !== null ? (
          <div
            className={styles.feedback}
            role="status"
            aria-label="Message status"
            data-tone="error"
          >
            <span>Message not sent. Your draft is safe.</span>
            <button
              type="button"
              aria-label="Retry message"
              onClick={() => void deliver(deliveryError.submission, deliveryError)}
            >
              <RotateCcw aria-hidden="true" size={15} />
              Retry
            </button>
          </div>
        ) : null}
        {queueReconciliation !== null ? (
          <div
            className={styles.feedback}
            role="status"
            aria-label="Queue reconciliation"
            data-tone={queueReconciliation.kind === "failed" ? "error" : "neutral"}
          >
            {queueReconciliation.kind === "failed" ? (
              <>
                <span>Queued follow-up was not cleared.</span>
                <button
                  type="button"
                  aria-label="Retry clearing follow-up"
                  onClick={() => void clearQueue(
                    queueReconciliation.operation.restoreToDraft,
                    queueReconciliation.operation,
                  )}
                >
                  <RotateCcw aria-hidden="true" size={15} />
                  Retry
                </button>
              </>
            ) : (
              <>
                <span>Previous follow-up saved: {queueReconciliation.operation.queued.text}</span>
                <button
                  type="button"
                  aria-label="Append cleared follow-up"
                  onClick={() => appendClearedFollowUp(queueReconciliation.operation)}
                >
                  Append
                </button>
                <button
                  type="button"
                  aria-label="Dismiss cleared follow-up"
                  onClick={() => dismissClearedFollowUp(queueReconciliation.operation)}
                >
                  Dismiss
                </button>
              </>
            )}
          </div>
        ) : null}
        <span className={styles.srOnly} role="status" aria-label="Turn status" aria-live="polite">
          {stopping ? "Stopping after current action" : turnError ? "Stop request failed" : ""}
        </span>
      </div>
    </section>
  );
}
