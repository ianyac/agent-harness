import { Check, Copy } from "lucide-react";
import { useState } from "react";

import styles from "./conversation.module.css";

export type CopyText = (text: string) => void | Promise<void>;

export async function copyToClipboard(text: string): Promise<void> {
  if (typeof navigator === "undefined" || navigator.clipboard?.writeText === undefined) {
    throw new Error("Clipboard access is unavailable.");
  }
  await navigator.clipboard.writeText(text);
}

type CodeBlockProps = {
  readonly code: string;
  readonly language?: string;
  readonly copyText?: CopyText;
};

const languageLabels: Readonly<Record<string, string>> = {
  bash: "Shell",
  css: "CSS",
  diff: "Diff",
  html: "HTML",
  js: "JavaScript",
  javascript: "JavaScript",
  json: "JSON",
  jsx: "JSX",
  markdown: "Markdown",
  md: "Markdown",
  py: "Python",
  python: "Python",
  sh: "Shell",
  shell: "Shell",
  ts: "TypeScript",
  tsx: "TSX",
  typescript: "TypeScript",
};

function diffLineType(line: string): "addition" | "removal" | "hunk" | "meta" | "context" {
  if (line.startsWith("@@")) return "hunk";
  if (line.startsWith("+++") || line.startsWith("---")) return "meta";
  if (line.startsWith("+")) return "addition";
  if (line.startsWith("-")) return "removal";
  return "context";
}

export function CodeBlock({ code, language = "text", copyText = copyToClipboard }: CodeBlockProps) {
  const [copyStatus, setCopyStatus] = useState<"idle" | "copied" | "error">("idle");
  const normalizedLanguage = language.toLocaleLowerCase();
  const label = languageLabels[normalizedLanguage] ?? `${language} code`;
  const regionLabel = normalizedLanguage === "diff" ? "Diff" : label.endsWith("code") ? label : `${label} code`;
  const diffLines = normalizedLanguage === "diff" ? code.split("\n") : null;

  const copy = async () => {
    try {
      await copyText(code);
      setCopyStatus("copied");
    } catch {
      setCopyStatus("error");
    }
  };

  return (
    <section className={styles.codeBlock} aria-label={regionLabel}>
      <div className={styles.codeToolbar}>
        <span>{normalizedLanguage === "text" ? "Code" : label}</span>
        <span
          className={styles.copyFeedback}
          data-status={copyStatus}
          role="status"
          aria-label="Code copy status"
          aria-live="polite"
          aria-atomic="true"
        >
          {copyStatus === "copied" ? "Copied" : copyStatus === "error" ? "Copy failed. Try again." : ""}
        </span>
        <button
          type="button"
          className={styles.copyButton}
          aria-label={
            copyStatus === "copied"
              ? "Code copied"
              : copyStatus === "error"
                ? "Retry copy code"
                : "Copy code"
          }
          onClick={() => void copy()}
        >
          {copyStatus === "copied" ? <Check aria-hidden="true" size={15} /> : <Copy aria-hidden="true" size={15} />}
          <span aria-hidden="true">{copyStatus === "copied" ? "Copied" : copyStatus === "error" ? "Retry" : "Copy"}</span>
        </button>
      </div>
      {diffLines !== null ? (
        <pre className={styles.diff}><code>
          {diffLines.map((line, index) => {
            const type = diffLineType(line);
            return (
              <span
                key={`${index}-${line}`}
                className={styles[`diff${type[0].toUpperCase()}${type.slice(1)}`]}
                data-diff-line={type}
              >
                {line || " "}
                {index < diffLines.length - 1 ? "\n" : null}
              </span>
            );
          })}
        </code></pre>
      ) : (
        <pre><code className={normalizedLanguage === "text" ? undefined : `language-${normalizedLanguage}`}>{code}</code></pre>
      )}
    </section>
  );
}
