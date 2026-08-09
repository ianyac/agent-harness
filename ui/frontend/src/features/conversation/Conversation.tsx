import { ArrowDown } from "lucide-react";
import { useEffect, useId, useLayoutEffect, useMemo, useRef, useState } from "react";

import { ActivityCard } from "../activity/ActivityCard";
import { groupActivities } from "../activity/groupActivities";
import type {
  ActivityGroup,
  GroupedTimelineItem,
  TimelineMarker,
} from "../activity/groupActivities";
import { PermissionCard } from "../permissions/PermissionCard";
import { PlanReviewCard } from "../plan-review/PlanReviewCard";
import type { ActivityItem, ClientEvent, TranscriptState } from "../../protocol/types";
import type { CopyText } from "./CodeBlock";
import { ConversationSearch } from "./ConversationSearch";
import type { SearchableMessage } from "./ConversationSearch";
import { Message, visibleMessage } from "./Message";
import styles from "./conversation.module.css";

type ConversationProps = {
  readonly state: TranscriptState;
  readonly openInspector: (activityId: string) => void;
  readonly copyText?: CopyText;
  readonly ownsSearchShortcut?: boolean;
  readonly onSessionEvent?: (event: ClientEvent) => void | Promise<void>;
};

const NEAR_BOTTOM_PX = 80;

function isNearBottom(element: HTMLElement): boolean {
  return element.scrollHeight - element.clientHeight - element.scrollTop <= NEAR_BOTTOM_PX;
}

function isActivityGroup(item: GroupedTimelineItem): item is ActivityGroup {
  return Array.isArray(item);
}

export function Conversation({
  state,
  openInspector,
  copyText,
  ownsSearchShortcut = false,
  onSessionEvent = () => {},
}: ConversationProps) {
  const instanceId = useId().replaceAll(":", "");
  const scrollerRef = useRef<HTMLDivElement>(null);
  const wasNearBottom = useRef(true);
  const focusOrigin = useRef<HTMLElement | null>(null);
  const previousRunning = useRef(state.running);
  const [hasNewMessages, setHasNewMessages] = useState(false);
  const [searchOpen, setSearchOpen] = useState(false);
  const [announcement, setAnnouncement] = useState("");
  const visibleMessages = useMemo(
    () => state.messages.flatMap((message, messageIndex) => {
      const visible = visibleMessage(message);
      return visible === null ? [] : [{ ...visible, messageIndex }];
    }),
    [state.messages],
  );
  const reservedMessageIndices = new Set(state.timeline.flatMap((item) =>
    item.kind === "assistant" && item.messageIndex !== null ? [item.messageIndex] : [],
  ));
  const ordinaryMessages = visibleMessages.filter(
    (message) => !reservedMessageIndices.has(message.messageIndex),
  );
  const orderedActivities = state.activityOrder.flatMap((id) => {
    const item = state.activities[id];
    return item === undefined ? [] : [item];
  });
  const resolvedTimeline: Array<ActivityItem | TimelineMarker> = [];
  for (const item of state.timeline) {
    if (item.kind !== "activity") {
      resolvedTimeline.push(item);
      continue;
    }
    const activity = state.activities[item.activityId];
    if (activity !== undefined) resolvedTimeline.push(activity);
  }
  const timelineItems: GroupedTimelineItem[] = state.timeline.length === 0
    ? groupActivities(orderedActivities)
    : groupActivities(resolvedTimeline);
  const timelineAssistants = timelineItems.flatMap((item, timelineIndex) =>
    isActivityGroup(item) || item.kind !== "assistant"
      ? []
      : [{ ...item, timelineIndex }],
  );
  const searchMessages: SearchableMessage[] = ordinaryMessages.map((message) => ({
    id: `${instanceId}-message-${message.messageIndex}`,
    text: message.content,
  }));
  searchMessages.push(...timelineAssistants.map((item) => ({
    id: `${instanceId}-timeline-${item.timelineIndex}`,
    text: item.text,
  })));
  if (state.timeline.length === 0 && state.streamingText !== "") {
    searchMessages.push({ id: `${instanceId}-streaming`, text: state.streamingText });
  }
  const contentVersion = `${state.generation}:${state.lastSequence}:${visibleMessages.length}:${state.streamingText.length}:${state.activityOrder.length}`;

  useEffect(() => {
    if (!ownsSearchShortcut) return;
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
  }, [ownsSearchShortcut, searchOpen]);

  useEffect(() => {
    if (previousRunning.current && !state.running) {
      setAnnouncement(state.terminal?.kind === "completed" ? "Response complete" : "");
    } else if (state.running) {
      setAnnouncement("");
    }
    previousRunning.current = state.running;
  }, [state.running, state.terminal]);

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
          {ordinaryMessages.map((message) => (
            <Message
              key={`${message.messageIndex}-${message.role}`}
              id={`${instanceId}-message-${message.messageIndex}`}
              role={message.role}
              content={message.content}
              copyText={copyText}
            />
          ))}
          {timelineItems.map((item, timelineIndex) => {
            if (isActivityGroup(item)) {
              return (
                <ActivityCard
                  key={item[0].activityId}
                  activities={item}
                  openInspector={openInspector}
                />
              );
            }
            if (item.kind === "boundary") return null;
            if (item.kind === "context") return null;
            if (item.kind === "permission") {
              return (
                <PermissionCard
                  key={`permission-${item.request.requestId}`}
                  request={item.request}
                  resolution={item.resolution}
                  active={state.permission?.requestId === item.request.requestId}
                  turnRunning={state.running}
                  safety={state.safety}
                  onAnswer={onSessionEvent}
                />
              );
            }
            if (item.kind === "plan_review") {
              return (
                <PlanReviewCard
                  key={`plan-review-${item.request.requestId}`}
                  request={item.request}
                  resolution={item.resolution}
                  active={state.planReview?.requestId === item.request.requestId}
                  turnRunning={state.running}
                  onAnswer={onSessionEvent}
                />
              );
            }
            return (
              <Message
                key={`timeline-${timelineIndex}`}
                id={`${instanceId}-timeline-${timelineIndex}`}
                role="assistant"
                content={item.text}
                streaming={item.messageIndex === null && state.running}
                copyText={copyText}
              />
            );
          })}
          {state.timeline.length === 0 && state.streamingText !== "" ? (
            <Message
              id={`${instanceId}-streaming`}
              role="assistant"
              content={state.streamingText}
              streaming
              copyText={copyText}
            />
          ) : null}
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
