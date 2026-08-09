import { Bot } from "lucide-react";

import type { HarnessMessage, JsonValue } from "../../protocol/types";
import type { CopyText } from "./CodeBlock";
import { MarkdownContent } from "./MarkdownContent";
import styles from "./conversation.module.css";

export type VisibleMessage = {
  readonly role: "user" | "assistant";
  readonly content: string;
};

function textFromContent(content: JsonValue | undefined): string | null {
  if (typeof content === "string") return content;
  if (!Array.isArray(content)) return null;
  const parts = content.flatMap((part) => {
    if (typeof part === "string") return [part];
    if (part === null || Array.isArray(part) || typeof part !== "object") return [];
    return typeof part.text === "string" ? [part.text] : [];
  });
  return parts.length === 0 ? null : parts.join("\n");
}

export function visibleMessage(message: HarnessMessage): VisibleMessage | null {
  const role = message.role;
  if (role !== "user" && role !== "assistant") return null;
  const content = textFromContent(message.content);
  if (content === null || (role === "assistant" && content.trim() === "")) return null;
  return { role, content };
}

type MessageProps = VisibleMessage & {
  readonly id: string;
  readonly streaming?: boolean;
  readonly copyText?: CopyText;
};

export function Message({ id, role, content, streaming = false, copyText }: MessageProps) {
  const label = role === "user" ? "User message" : "Assistant message";
  return (
    <article
      id={id}
      className={styles.message}
      data-message-role={role}
      data-streaming={streaming ? "true" : undefined}
      aria-label={label}
      tabIndex={-1}
    >
      {role === "assistant" ? (
        <span className={styles.avatar} aria-hidden="true"><Bot size={17} /></span>
      ) : null}
      <div className={styles.messageBody}>
        {role === "user" ? <p>{content}</p> : <MarkdownContent content={content} copyText={copyText} />}
      </div>
    </article>
  );
}
