import { ArrowUp, Paperclip, RotateCcw, Square, X } from "lucide-react";
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
};

type Submission = Extract<ClientEvent, { type: "send_message" | "queue_message" }>;

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
}: ComposerProps) {
  const [draft, setDraft] = useDraft(sessionId, {
    storage: draftStorage,
    backupDelayMs,
    memory: draftMemory,
  });
  const [mode, setMode] = useState<TurnMode>("base");
  const [suggestionsDismissed, setSuggestionsDismissed] = useState(false);
  const [activeSuggestion, setActiveSuggestion] = useState(0);
  const [pending, setPending] = useState<Submission | null>(null);
  const [messageError, setMessageError] = useState(false);
  const [turnError, setTurnError] = useState(false);
  const composing = useRef(false);
  const textboxRef = useRef<HTMLTextAreaElement>(null);
  const stopRef = useRef<HTMLButtonElement>(null);
  const previousRunning = useRef(running);
  const listboxId = useId();
  const slashQuery = draft.startsWith("/") ? draft.split(/\s/, 1)[0].toLocaleLowerCase() : "";
  const matchingCommands = useMemo(
    () => slashQuery === "" ? [] : commands.filter(({ command }) => command.startsWith(slashQuery)),
    [slashQuery],
  );
  const suggestionsOpen = !suggestionsDismissed && matchingCommands.length > 0;
  const blank = draft.trim() === "";

  useEffect(() => {
    setMode("base");
    setSuggestionsDismissed(false);
    setActiveSuggestion(0);
    setPending(null);
    setMessageError(false);
    setTurnError(false);
  }, [sessionId]);

  useEffect(() => {
    if (previousRunning.current && !running) textboxRef.current?.focus();
    previousRunning.current = running;
  }, [running]);

  useEffect(() => {
    setActiveSuggestion(0);
    setSuggestionsDismissed(false);
  }, [draft]);

  const deliver = async (submission: Submission) => {
    setPending(submission);
    setMessageError(false);
    try {
      await onEvent(submission);
      setDraft((current) => current === submission.text ? "" : current);
      setPending(null);
    } catch {
      setMessageError(true);
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

  const clearQueue = async () => {
    try {
      await onEvent({ type: "clear_queued_message" });
      setDraft(queued?.text ?? "");
      setMode(queued?.mode ?? "base");
      textboxRef.current?.focus();
    } catch {
      setMessageError(true);
    }
  };

  const chooseCommand = (index: number) => {
    const selected = matchingCommands[index];
    if (selected === undefined) return;
    setMode(selected.mode);
    setDraft(draft.replace(/^\/\S+\s*/, ""));
    setSuggestionsDismissed(true);
    textboxRef.current?.focus();
  };

  const requestStop = async () => {
    setTurnError(false);
    try {
      await onStop();
    } catch {
      setTurnError(true);
    }
  };

  const queueActionName = queued === null ? "Queue message" : "Update queued message";

  return (
    <section
      className={styles.composerRegion}
      aria-label="Message composer"
      onKeyDown={(event) => {
        if (event.key !== "Escape") return;
        event.preventDefault();
        if (suggestionsOpen) {
          setSuggestionsDismissed(true);
        } else if (running) {
          stopRef.current?.focus();
        }
      }}
    >
      <div className={styles.composer}>
        {queued !== null ? (
          <div className={styles.queued} role="status" aria-label="Queued follow-up">
            <div className={styles.queuedCopy}>
              <span className={styles.queuedLabel}>Queued · {queued.mode === "plan" ? "Plan" : "Base"}</span>
              <span className={styles.queuedText}>{queued.text}</span>
            </div>
            <button type="button" aria-label="Edit queued follow-up" onClick={() => void clearQueue()}>
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
            aria-keyshortcuts="Meta+Enter"
            aria-autocomplete="list"
            aria-controls={suggestionsOpen ? listboxId : undefined}
            aria-activedescendant={suggestionsOpen ? `${listboxId}-${activeSuggestion}` : undefined}
            rows={3}
            placeholder={running ? "Add a follow-up…" : "Message Agent Harness…"}
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            onCompositionStart={() => {
              composing.current = true;
            }}
            onCompositionEnd={() => {
              composing.current = false;
            }}
            onKeyDown={(event) => {
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
                event.metaKey &&
                !event.altKey &&
                !event.ctrlKey &&
                !event.shiftKey &&
                !event.nativeEvent.isComposing &&
                !composing.current
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
              onClick={() => setMode("base")}
            >
              Base
            </button>
            <button
              type="button"
              aria-label="Plan mode"
              aria-pressed={mode === "plan"}
              onClick={() => setMode("plan")}
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

        {messageError && pending !== null ? (
          <div className={styles.feedback} role="status" aria-label="Message status">
            <span>Message not sent. Your draft is safe.</span>
            <button type="button" aria-label="Retry message" onClick={() => void deliver(pending)}>
              <RotateCcw aria-hidden="true" size={15} />
              Retry
            </button>
          </div>
        ) : null}
        <span className={styles.srOnly} role="status" aria-label="Turn status" aria-live="polite">
          {stopping ? "Stopping after current action" : turnError ? "Stop request failed" : ""}
        </span>
      </div>
    </section>
  );
}
