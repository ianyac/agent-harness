import type { JsonObject, JsonValue, SafetySnapshot } from "../../protocol/types";
import styles from "./inspector.module.css";

function objectValue(value: JsonValue | undefined): JsonObject | null {
  return value !== null && value !== undefined && !Array.isArray(value) && typeof value === "object"
    ? value
    : null;
}

function exactString(value: JsonValue | undefined, allowed?: readonly string[]): string {
  if (typeof value !== "string" || value.trim() === "") return "Unknown";
  if (allowed !== undefined && !allowed.includes(value)) return "Unknown";
  return value;
}

function exactBoolean(value: JsonValue | undefined): string {
  return typeof value === "boolean" ? value ? "Yes" : "No" : "Unknown";
}

function label(value: string): string {
  return value.replaceAll("_", " ");
}

function sentenceCase(value: string): string {
  return value === "Unknown" ? value : `${value[0]?.toLocaleUpperCase() ?? ""}${value.slice(1)}`;
}

type SafetySummaryProps = {
  readonly safety: SafetySnapshot | null;
};

export function SafetySummary({ safety }: SafetySummaryProps) {
  const source = safety ?? {};
  const tools = objectValue(source.tools);
  const toolRows = tools === null
    ? []
    : Object.keys(tools).sort((a, b) => a.localeCompare(b)).map((name) => {
        const detail = objectValue(tools[name]);
        return {
          name,
          decision: exactString(detail?.decision, ["allow", "ask", "deny"]),
          readOnly: exactBoolean(detail?.read_only),
          networkEgress: exactBoolean(detail?.network_egress),
        };
      });

  return (
    <section className={styles.summary} aria-labelledby="inspector-safety-title">
      <h3 id="inspector-safety-title">Safety</h3>
      <dl className={styles.definitionGrid}>
        <div><dt>Sandbox backend</dt><dd>{exactString(source.sandbox_backend)}</dd></div>
        <div><dt>Workspace write boundary</dt><dd>{exactString(source.workspace_write_boundary)}</dd></div>
        <div><dt>Read breadth</dt><dd>{exactString(source.read_breadth)}</dd></div>
        <div><dt>Network policy</dt><dd>{sentenceCase(exactString(source.network_policy, ["allow", "deny"]))}</dd></div>
      </dl>
      <div className={styles.tableScroller}>
        <table className={styles.toolTable}>
          <caption>Registered tool decisions</caption>
          <thead>
            <tr><th>Tool</th><th>Decision</th><th>Read only</th><th>Network egress</th></tr>
          </thead>
          <tbody>
            {toolRows.length === 0 ? (
              <tr><th scope="row">Unknown</th><td>Unknown</td><td>Unknown</td><td>Unknown</td></tr>
            ) : toolRows.map((tool) => (
              <tr key={tool.name}>
                <th scope="row">{label(tool.name)}</th>
                <td>{sentenceCase(tool.decision)}</td>
                <td>{tool.readOnly}</td>
                <td>{tool.networkEgress}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}
