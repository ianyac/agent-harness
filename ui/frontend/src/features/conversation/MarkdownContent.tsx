import { Check, Copy } from "lucide-react";
import { Children, isValidElement, useState } from "react";
import type { ReactElement, ReactNode } from "react";
import ReactMarkdown, { defaultUrlTransform } from "react-markdown";
import remarkGfm from "remark-gfm";

import { CodeBlock, copyToClipboard } from "./CodeBlock";
import type { CopyText } from "./CodeBlock";
import styles from "./conversation.module.css";
import markdownStyles from "./markdown.module.css";

type MarkdownContentProps = {
  readonly content: string;
  readonly copyText?: CopyText;
};

function readableHref(href: string): string {
  try {
    return decodeURIComponent(href);
  } catch {
    return href;
  }
}

function isLocalPath(href: string): boolean {
  const path = readableHref(href);
  return /^file:\/\//i.test(path) ||
    (path.startsWith("/") && !path.startsWith("//")) ||
    path.startsWith("~/") ||
    path.startsWith("./") ||
    path.startsWith("../") ||
    /^[A-Za-z]:[\\/]/.test(path);
}

function isExternalLink(href: string): boolean {
  return /^https?:\/\//i.test(href) || href.startsWith("//");
}

function safeUrlTransform(url: string, key: string): string {
  if (key === "href" && isLocalPath(url)) return url;
  return defaultUrlTransform(url);
}

function LocalPath({ href, copyText }: { readonly href: string; readonly copyText: CopyText }) {
  const path = readableHref(href);
  const [copyStatus, setCopyStatus] = useState<"idle" | "copied" | "error">("idle");
  const copy = async () => {
    try {
      await copyText(path);
      setCopyStatus("copied");
    } catch {
      setCopyStatus("error");
    }
  };

  return (
    <span className={styles.localPath}>
      <code>{path}</code>
      <button
        type="button"
        className={styles.inlineCopyButton}
        aria-label={
          copyStatus === "copied"
            ? "Path copied"
            : copyStatus === "error"
              ? "Retry copy path"
              : "Copy path"
        }
        onClick={() => void copy()}
      >
        {copyStatus === "copied" ? <Check aria-hidden="true" size={14} /> : <Copy aria-hidden="true" size={14} />}
      </button>
      <span
        className={styles.copyFeedback}
        data-status={copyStatus}
        role="status"
        aria-label="Path copy status"
        aria-live="polite"
        aria-atomic="true"
      >
        {copyStatus === "copied" ? "Copied" : copyStatus === "error" ? "Copy failed. Try again." : ""}
      </span>
    </span>
  );
}

function codeText(child: ReactNode): string {
  return Children.toArray(child).join("").replace(/\n$/, "");
}

export function MarkdownContent({ content, copyText = copyToClipboard }: MarkdownContentProps) {
  return (
    <div className={`${styles.markdown} ${markdownStyles.markdown}`}>
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        urlTransform={safeUrlTransform}
        components={{
          a({ href = "", children, node: _node, ...props }) {
            if (href === "") return <span>{children}</span>;
            if (isExternalLink(href) || href.startsWith("#")) {
              return (
                <a
                  {...props}
                  href={href}
                  target={isExternalLink(href) ? "_blank" : undefined}
                  rel={isExternalLink(href) ? "noreferrer" : undefined}
                >
                  {children}
                </a>
              );
            }
            // Local paths and relative hrefs would hijack SPA navigation;
            // both render as copyable text instead of live anchors.
            return <LocalPath href={href} copyText={copyText} />;
          },
          pre({ children }) {
            const child = Children.only(children);
            if (!isValidElement(child)) return <pre>{children}</pre>;
            const code = child as ReactElement<{ className?: string; children?: ReactNode }>;
            const language = /language-([^\s]+)/.exec(code.props.className ?? "")?.[1];
            return <CodeBlock code={codeText(code.props.children)} language={language} copyText={copyText} />;
          },
          table({ children, node: _node, ...props }) {
            return (
              <div className={markdownStyles.tableScroll}>
                <table {...props}>{children}</table>
              </div>
            );
          },
        }}
      >
        {content}
      </ReactMarkdown>
    </div>
  );
}
