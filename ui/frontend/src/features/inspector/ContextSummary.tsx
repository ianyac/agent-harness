import type { JsonObject, JsonValue } from "../../protocol/types";
import styles from "./inspector.module.css";

function modeLabel(value: string): string {
  if (value === "compaction") return "Compaction";
  if (value === "folding") return "Folding";
  return "Unknown";
}

function knownMode(value: JsonValue | undefined): string {
  return typeof value === "string" ? modeLabel(value) : "Unknown";
}

function exactCount(value: JsonValue | undefined): string {
  return typeof value === "number" && Number.isFinite(value) && value >= 0
    ? String(value)
    : "Unknown";
}

function sortedValue(value: JsonValue): JsonValue {
  if (Array.isArray(value)) return value.map(sortedValue);
  if (value === null || typeof value !== "object") return value;
  return Object.fromEntries(
    Object.keys(value).sort((a, b) => a.localeCompare(b)).map((key) => [key, sortedValue(value[key])]),
  );
}

function serializeContext(value: JsonObject): string {
  return JSON.stringify(sortedValue(value), null, 2);
}

type ContextSummaryProps = {
  readonly contextMode: string;
  readonly latestContext: JsonObject | null;
};

export function ContextSummary({ contextMode, latestContext }: ContextSummaryProps) {
  const source = latestContext ?? {};
  return (
    <section className={styles.summary} aria-labelledby="inspector-context-title">
      <h3 id="inspector-context-title">Context</h3>
      <dl className={styles.definitionGrid}>
        <div><dt>Configured mode</dt><dd>{modeLabel(contextMode)}</dd></div>
        <div><dt>Latest management event</dt><dd>{knownMode(source.mode)}</dd></div>
        <div><dt>Used tokens</dt><dd>{exactCount(source.used_tokens)}</dd></div>
        <div><dt>Summarized messages</dt><dd>{exactCount(source.summarized_messages)}</dd></div>
      </dl>
      <details className={styles.payloadDetails}>
        <summary>Exact latest context</summary>
        <pre>{latestContext === null ? "Unknown" : serializeContext(latestContext)}</pre>
      </details>
    </section>
  );
}
