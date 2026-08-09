import { ArrowDown } from "lucide-react";
import { useEffect, useId, useLayoutEffect, useMemo, useRef, useState } from "react";

import { ActivityCard } from "../activity/ActivityCard";
import { groupActivities } from "../activity/groupActivities";
import type { TranscriptState } from "../../protocol/types";
import type { CopyText } from "./CodeBlock";
import { ConversationSearch } from "./ConversationSearch";
import type { SearchableMessage } from "./ConversationSearch";
import { Message, visibleMessage } from "./Message";
import styles from "./conversation.module.css";

type ConversationProps = {
  readonly state: TranscriptState;
  readonly openInspector: (activityId: string) => void;
  readonly copyText?: CopyText;
};

const NEAR_BOTTOM_PX = 80;

function isNearBottom(element: HTMLElement): boolean {
  return element.scrollHeight - element.clientHeight - element.scrollTop <= NEAR_BOTTOM_PX;
}

export function Conversation({ state, openInspector, copyText }: ConversationProps) {
  const instanceId = useId().replaceAll(":", "");
  const scrollerRef = useRef<HTMLDivElement>(null);
  const wasNearBottom = useRef(true);
  const focusOrigin = useRef<HTMLElement | null>(null);
  const previousRunning = useRef(state.running);
  const [hasNewMessages, setHasNewMessages] = useState(false);
  const [searchOpen, setSearchOpen] = useState(false);
  const [announcement, setAnnouncement] = useState("");
  const visibleMessages = useMemo(
    () => state.messages.map(visibleMessage).filter((message) => message !== null),
    [state.messages],
  );
  const orderedActivities = state.activityOrder.flatMap((id) => {
    const item = state.activities[id];
    return item === undefined ? [] : [item];
  });
  const activityGroups = groupActivities(orderedActivities);
  const searchMessages: SearchableMessage[] = visibleMessages.map((message, index) => ({
    id: `${instanceId}-message-${index}`,
    text: message.content,
  }));
  if (state.streamingText !== "") {
    searchMessages.push({ id: `${instanceId}-streaming`, text: state.streamingText });
  }
  const contentVersion = `${state.generation}:${state.lastSequence}:${visibleMessages.length}:${state.streamingText.length}:${state.activityOrder.length}`;

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (!event.metaKey || event.ctrlKey || event.altKey || event.shiftKey || event.repeat) return;
      if (event.key.toLocaleLowerCase() !== "f") return;
      event.preventDefault();
      if (!searchOpen && document.activeElement instanceof HTMLElement) {
        focusOrigin.current = document.activeElement;
      }
      setSearchOpen(true);
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [searchOpen]);

  useEffect(() => {
    if (previousRunning.current && !state.running) {
      setAnnouncement("Response complete");
    } else if (state.running) {
      setAnnouncement("");
    }
    previousRunning.current = state.running;
  }, [state.running]);

  useLayoutEffect(() => {
    const scroller = scrollerRef.current;
    if (scroller === null) return;
    if (wasNearBottom.current) {
      scroller.scrollTop = scroller.scrollHeight;
      setHasNewMessages(false);
    } else {
      setHasNewMessages(true);
    }
  }, [contentVersion]);

  const closeSearch = (matchedMessageId: string | null) => {
    const target = matchedMessageId === null
      ? focusOrigin.current
      : document.getElementById(matchedMessageId);
    setSearchOpen(false);
    focusOrigin.current = null;
    if (target instanceof HTMLElement && target.isConnected) target.focus();
  };

  const scrollToLatest = () => {
    const scroller = scrollerRef.current;
    if (scroller === null) return;
    scroller.scrollTop = scroller.scrollHeight;
    wasNearBottom.current = true;
    setHasNewMessages(false);
  };

  return (
    <div className={styles.conversation}>
      {searchOpen ? <ConversationSearch messages={searchMessages} onClose={closeSearch} /> : null}
      <div
        ref={scrollerRef}
        className={styles.scroller}
        role="log"
        aria-label="Conversation transcript"
        aria-busy={state.running}
        aria-live="off"
        onScroll={(event) => {
          wasNearBottom.current = isNearBottom(event.currentTarget);
          if (wasNearBottom.current) setHasNewMessages(false);
        }}
      >
        <div className={styles.transcript}>
          {visibleMessages.map((message, index) => (
            <Message
              key={`${index}-${message.role}`}
              id={`${instanceId}-message-${index}`}
              role={message.role}
              content={message.content}
              copyText={copyText}
            />
          ))}
          {activityGroups.map((group) => (
            <ActivityCard key={group[0].activityId} activities={group} openInspector={openInspector} />
          ))}
          {state.streamingText === "" ? null : (
            <Message
              id={`${instanceId}-streaming`}
              role="assistant"
              content={state.streamingText}
              streaming
              copyText={copyText}
            />
          )}
        </div>
      </div>
      <span className={styles.srOnly} role="status" aria-label="Conversation update">
        {announcement}
      </span>
      {hasNewMessages ? (
        <button type="button" className={styles.newMessages} onClick={scrollToLatest}>
          <ArrowDown aria-hidden="true" size={16} />
          New messages
        </button>
      ) : null}
    </div>
  );
}
