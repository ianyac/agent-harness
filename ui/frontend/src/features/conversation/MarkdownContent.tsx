import { Check, Copy } from "lucide-react";
import { Children, cloneElement, isValidElement, useState } from "react";
import type { ComponentPropsWithoutRef, ReactElement, ReactNode } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

import { CodeBlock, copyToClipboard } from "./CodeBlock";
import type { CopyText } from "./CodeBlock";
import styles from "./conversation.module.css";

type MarkdownContentProps = {
  readonly content: string;
  readonly copyText?: CopyText;
};

function isLocalPath(href: string): boolean {
  return href.startsWith("/") || href.startsWith("~/") || href.startsWith("file://");
}

function isExternalLink(href: string): boolean {
  return /^https?:\/\//i.test(href);
}

function LocalPath({ href, copyText }: { readonly href: string; readonly copyText: CopyText }) {
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    try {
      await copyText(href);
      setCopied(true);
    } catch {
      setCopied(false);
    }
  };

  return (
    <span className={styles.localPath}>
      <code>{href}</code>
      <button
        type="button"
        className={styles.inlineCopyButton}
        aria-label={copied ? "Path copied" : "Copy path"}
        onClick={() => void copy()}
      >
        {copied ? <Check aria-hidden="true" size={14} /> : <Copy aria-hidden="true" size={14} />}
      </button>
    </span>
  );
}

function codeText(child: ReactNode): string {
  return Children.toArray(child).join("").replace(/\n$/, "");
}

export function MarkdownContent({ content, copyText = copyToClipboard }: MarkdownContentProps) {
  return (
    <div className={styles.markdown}>
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          a({ href = "", children, ...props }: ComponentPropsWithoutRef<"a">) {
            if (isLocalPath(href)) return <LocalPath href={href} copyText={copyText} />;
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
          },
          pre({ children }) {
            const child = Children.only(children);
            if (!isValidElement(child)) return <pre>{children}</pre>;
            const code = child as ReactElement<{ className?: string; children?: ReactNode }>;
            const language = /language-([^\s]+)/.exec(code.props.className ?? "")?.[1];
            return <CodeBlock code={codeText(code.props.children)} language={language} copyText={copyText} />;
          },
          code({ className, children, ...props }) {
            return <code {...props} className={className}>{children}</code>;
          },
        }}
      >
        {content}
      </ReactMarkdown>
    </div>
  );
}
