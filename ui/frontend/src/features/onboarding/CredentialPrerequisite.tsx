type CredentialPrerequisiteProps = {
  readonly pending: boolean;
  readonly onRetry: () => void;
};

export function CredentialPrerequisite({ pending, onRetry }: CredentialPrerequisiteProps) {
  return (
    <section aria-labelledby="credential-heading">
      <h1 id="credential-heading">Sign in required</h1>
      <p>Agent Harness uses your existing local Codex sign-in.</p>
      <p>Run <code>codex login</code> in a terminal, then retry the same local session.</p>
      <button type="button" disabled={pending} onClick={onRetry}>Retry</button>
    </section>
  );
}
