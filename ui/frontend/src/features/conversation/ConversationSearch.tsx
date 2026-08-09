import { ChevronDown, ChevronUp, Search, X } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";

import styles from "./conversation.module.css";

export type SearchableMessage = {
  readonly id: string;
  readonly text: string;
};

type ConversationSearchProps = {
  readonly messages: readonly SearchableMessage[];
  readonly onClose: (matchedMessageId: string | null) => void;
};

type SearchMatch = {
  readonly messageId: string;
  readonly offset: number;
};

function findMatches(messages: readonly SearchableMessage[], query: string): SearchMatch[] {
  const needle = query.toLocaleLowerCase();
  if (needle === "") return [];
  return messages.flatMap((message) => {
    const haystack = message.text.toLocaleLowerCase();
    const matches: SearchMatch[] = [];
    let offset = 0;
    while (offset <= haystack.length - needle.length) {
      const index = haystack.indexOf(needle, offset);
      if (index === -1) break;
      matches.push({ messageId: message.id, offset: index });
      offset = index + Math.max(needle.length, 1);
    }
    return matches;
  });
}

export function ConversationSearch({ messages, onClose }: ConversationSearchProps) {
  const [query, setQuery] = useState("");
  const [activeIndex, setActiveIndex] = useState(0);
  const [searchFocused, setSearchFocused] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);
  const matches = useMemo(() => findMatches(messages, query), [messages, query]);
  const normalizedIndex = matches.length === 0 ? 0 : activeIndex % matches.length;
  const activeMatch = matches[normalizedIndex] ?? null;

  useEffect(() => {
    inputRef.current?.focus();
  }, []);

  useEffect(() => {
    if (activeMatch === null) return;
    document.getElementById(activeMatch.messageId)?.scrollIntoView?.({
      block: "center",
      behavior: "auto",
    });
  }, [activeMatch?.messageId, activeMatch?.offset]);

  const move = (direction: 1 | -1) => {
    if (matches.length === 0) return;
    setActiveIndex((index) => (index + direction + matches.length) % matches.length);
    inputRef.current?.focus();
  };

  const close = () => onClose(activeMatch?.messageId ?? null);

  return (
    <div className={styles.search} role="search" aria-label="Conversation search">
      <Search aria-hidden="true" size={17} />
      <input
        ref={inputRef}
        type="search"
        aria-label="Search conversation"
        style={searchFocused ? { outlineStyle: "solid" } : undefined}
        value={query}
        onFocus={() => setSearchFocused(true)}
        onBlur={() => setSearchFocused(false)}
        onChange={(event) => {
          setQuery(event.target.value);
          setActiveIndex(0);
        }}
        onKeyDown={(event) => {
          if (event.key === "Enter") {
            event.preventDefault();
            move(event.shiftKey ? -1 : 1);
          } else if (event.key === "Escape") {
            event.preventDefault();
            close();
          } else if (event.metaKey && event.key.toLocaleLowerCase() === "f") {
            event.preventDefault();
            event.currentTarget.select();
          }
        }}
      />
      <span className={styles.searchPosition} role="status" aria-label="Search result position">
        {matches.length === 0 ? "No matches" : `${normalizedIndex + 1} of ${matches.length}`}
      </span>
      <button
        type="button"
        aria-label="Previous match"
        disabled={matches.length === 0}
        onMouseDown={(event) => event.preventDefault()}
        onClick={() => move(-1)}
      >
        <ChevronUp aria-hidden="true" size={16} />
      </button>
      <button
        type="button"
        aria-label="Next match"
        disabled={matches.length === 0}
        onMouseDown={(event) => event.preventDefault()}
        onClick={() => move(1)}
      >
        <ChevronDown aria-hidden="true" size={16} />
      </button>
      <button type="button" aria-label="Close conversation search" onClick={close}>
        <X aria-hidden="true" size={16} />
      </button>
    </div>
  );
}
