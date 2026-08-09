import { FolderOpen, LoaderCircle, ShieldCheck } from "lucide-react";
import { useLayoutEffect, useRef, useState } from "react";

import { ApiError } from "../../api/http";
import type { PlatformAdapter } from "../../platform/types";
import type { BaseMode } from "../../protocol/types";
import type {
  ContextMode,
  CreateSessionAuthority,
  CreateSessionOptions,
  CreateSessionResult,
} from "../sessions/useSessions";
import { createSessionAuthority } from "../sessions/useSessions";
import { CredentialPrerequisite } from "./CredentialPrerequisite";
import styles from "./onboarding.module.css";

type OnboardingProps = {
  readonly serviceStatus: "checking" | "ready";
  readonly platform: PlatformAdapter;
  readonly defaultWorkspace: string;
  readonly defaultMode: BaseMode;
  readonly defaultContextMode: ContextMode;
  readonly onCreate: (
    options: CreateSessionOptions,
    authority: CreateSessionAuthority,
  ) => Promise<CreateSessionResult>;
  readonly onInvalidateCreate?: (authority: CreateSessionAuthority) => void;
  readonly authorityKey?: unknown;
};

const ignoreInvalidation = () => {};

function browserPathError(path: string): string | null {
  if (path === "") return "Choose a workspace to continue.";
  if (!path.startsWith("/") || path.includes("\0") || path.trim() !== path) {
    return "Enter an absolute workspace path without surrounding whitespace.";
  }
  return null;
}

export function Onboarding({
  serviceStatus,
  platform,
  defaultWorkspace,
  defaultMode,
  defaultContextMode,
  onCreate,
  onInvalidateCreate = ignoreInvalidation,
  authorityKey,
}: OnboardingProps) {
  const [workspace, setWorkspace] = useState(defaultWorkspace);
  const [mode, setMode] = useState<BaseMode>(defaultMode);
  const [contextMode, setContextMode] = useState<ContextMode>(defaultContextMode);
  const [error, setError] = useState<string | null>(null);
  const [credentialRequest, setCredentialRequest] = useState<CreateSessionOptions | null>(null);
  const [pending, setPending] = useState(false);
  const [complete, setComplete] = useState(false);
  const authorityRef = useRef<CreateSessionAuthority>(createSessionAuthority());
  const pendingRef = useRef<CreateSessionAuthority | null>(null);

  useLayoutEffect(() => {
    onInvalidateCreate(authorityRef.current);
    authorityRef.current = createSessionAuthority();
    pendingRef.current = null;
    setPending(false);
    setComplete(false);
    setCredentialRequest(null);
    setError(null);
    setWorkspace(defaultWorkspace);
    setMode(defaultMode);
    setContextMode(defaultContextMode);
  }, [authorityKey, defaultContextMode, defaultMode, defaultWorkspace, onInvalidateCreate, platform]);

  const invalidate = () => {
    const invalidated = authorityRef.current;
    onInvalidateCreate(invalidated);
    authorityRef.current = createSessionAuthority();
    if (pendingRef.current === invalidated) {
      pendingRef.current = null;
      setPending(false);
    }
    setCredentialRequest(null);
    setError(null);
  };

  const submit = async (request: CreateSessionOptions) => {
    if (pendingRef.current !== null) return;
    const authority = authorityRef.current;
    pendingRef.current = authority;
    setPending(true);
    setError(null);
    const result = await onCreate(request, authority);
    if (authorityRef.current !== authority) return;
    if (pendingRef.current === authority) pendingRef.current = null;
    setPending(false);
    if (result.ok) {
      setCredentialRequest(null);
      setComplete(true);
      return;
    }
    if (result.error instanceof ApiError && result.error.category === "credential_prerequisite") {
      setCredentialRequest(request);
      return;
    }
    setError(result.error.message);
  };

  if (complete) return null;
  if (serviceStatus === "checking") {
    return (
      <section className={styles.checking} aria-label="First run">
        <LoaderCircle aria-hidden="true" size={22} />
        <p role="status" aria-label="Checking local service">Checking local service</p>
      </section>
    );
  }
  if (credentialRequest !== null) {
    return (
      <div className={styles.card}>
        <CredentialPrerequisite
          pending={pending}
          onRetry={() => void submit(credentialRequest)}
        />
      </div>
    );
  }

  const chooseNativeWorkspace = async () => {
    if (pendingRef.current !== null) return;
    setError(null);
    try {
      const chosen = await platform.chooseWorkspace();
      if (chosen === null) return;
      invalidate();
      setWorkspace(chosen);
    } catch (value) {
      setError(value instanceof Error ? value.message : "The folder picker failed.");
    }
  };
  const start = () => {
    const validationError = browserPathError(workspace);
    if (validationError !== null) {
      setError(validationError);
      return;
    }
    void submit({ workspace, mode, contextMode, title: "New chat" });
  };

  return (
    <main className={styles.onboarding} aria-labelledby="first-run-title">
      <section className={styles.card}>
        <ShieldCheck aria-hidden="true" size={26} />
        <h1 id="first-run-title">Start locally</h1>
        <p>Processing and session files stay local to this computer.</p>
        <p className={styles.trust}>
          Choosing a workspace does not automatically run workspace configuration, hooks,
          MCP commands, or skill shell blocks.
        </p>

        {platform.kind === "tauri" ? (
          <div className={styles.field}>
            <span>Workspace</span>
            <button type="button" className={styles.secondaryButton} onClick={() => void chooseNativeWorkspace()}>
              <FolderOpen aria-hidden="true" size={18} /> Choose folder
            </button>
            {workspace === "" ? null : <code className={styles.workspace}>{workspace}</code>}
          </div>
        ) : (
          <label className={styles.field}>
            <span>Workspace path</span>
            <input
              aria-label="Workspace path"
              value={workspace}
              onChange={(event) => {
                invalidate();
                setWorkspace(event.target.value);
              }}
              spellCheck={false}
            />
          </label>
        )}

        <label className={styles.field}>
          <span>Permission mode</span>
          <select
            aria-label="Permission mode"
            value={mode}
            onChange={(event) => {
              invalidate();
              setMode(event.target.value as BaseMode);
            }}
          >
            <option value="default">Default</option>
            <option value="acceptAll">Accept all</option>
            <option value="readOnly">Read only</option>
          </select>
        </label>

        <fieldset className={styles.contextChoices}>
          <legend>Context management</legend>
          <label><input type="radio" name="first-context" checked={contextMode === "compaction"} onChange={() => { invalidate(); setContextMode("compaction"); }} /> Standard compaction</label>
          <label><input type="radio" name="first-context" checked={contextMode === "folding"} onChange={() => { invalidate(); setContextMode("folding"); }} /> Recoverable folding</label>
        </fieldset>

        {error === null ? null : <p role="alert" className={styles.error}>{error}</p>}
        <button type="button" className={styles.primaryButton} disabled={pending} onClick={start}>
          {pending ? "Starting…" : "Start local session"}
        </button>
      </section>
    </main>
  );
}
