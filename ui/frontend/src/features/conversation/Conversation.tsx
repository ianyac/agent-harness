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
import { messageHistory } from "../../protocol/history";
import type { ActivityItem, ClientEvent, TranscriptState } from "../../protocol/types";
import type { CopyText } from "./CodeBlock";
import { ConversationSearch } from "./ConversationSearch";
import type { SearchableMessage } from "./ConversationSearch";
import { Message, visibleMessage } from "./Message";
import styles from "./conversation.module.css";

type ConversationProps = {
  readonly state: TranscriptState;
  readonly optimisticUserMessages?: readonly OptimisticUserMessage[];
  readonly openInspector: (activityId: string) => void;
  readonly copyText?: CopyText;
  readonly ownsSearchShortcut?: boolean;
  readonly onSessionEvent?: (event: ClientEvent) => void | Promise<void>;
};

export type OptimisticUserMessage = {
  readonly submissionId: string;
  readonly content: string;
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
  optimisticUserMessages = [],
  openInspector,
  copyText,
  ownsSearchShortcut = false,
  onSessionEvent = () => {},
}: ConversationProps) {
  const instanceId = useId().replaceAll(":", "");
  const rootRef = useRef<HTMLDivElement>(null);
  const scrollerRef = useRef<HTMLDivElement>(null);
  const wasNearBottom = useRef(true);
  const focusOrigin = useRef<HTMLElement | null>(null);
  const previousRunning = useRef(state.running);
  const previousOptimisticCount = useRef(optimisticUserMessages.length);
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
  const history = useMemo(() => messageHistory(state.messages), [state.messages]);
  const reservedMessageIndices = new Set(state.timeline.flatMap((item) =>
    item.kind === "assistant" && item.messageIndex !== null ? [item.messageIndex] : [],
  ));
  const ordinaryMessages = visibleMessages.filter(
    (message) => !reservedMessageIndices.has(message.messageIndex),
  );
  const ordinaryByIndex = new Map(
    ordinaryMessages.map((message) => [message.messageIndex, message]),
  );
  const resolvedTimeline: Array<ActivityItem | TimelineMarker> = [];
  for (const item of state.timeline) {
    if (item.kind !== "activity") {
      resolvedTimeline.push(item);
      continue;
    }
    const activity = state.activities[item.activityId];
    if (activity !== undefined) resolvedTimeline.push(activity);
  }
  const timelineItems: GroupedTimelineItem[] = groupActivities(resolvedTimeline);
  const timelineAssistants = timelineItems.flatMap((item, timelineIndex) =>
    isActivityGroup(item) || item.kind !== "assistant"
      ? []
      : [{ ...item, timelineIndex }],
  );
  const optimisticBeforeTimeline = state.activeTurn !== null;
  const searchMessages: SearchableMessage[] = ordinaryMessages.map((message) => ({
    id: `${instanceId}-message-${message.messageIndex}`,
    text: message.content,
  }));
  searchMessages.push(...optimisticUserMessages.map((message) => ({
    id: `${instanceId}-optimistic-${message.submissionId}`,
    text: message.content,
  })));
  searchMessages.push(...timelineAssistants.map((item) => ({
    id: `${instanceId}-timeline-${item.timelineIndex}`,
    text: item.text,
  })));
  const optimisticVersion = optimisticUserMessages
    .map((message) => `${message.submissionId}:${message.content.length}`)
    .join("|");
  const timelineVersion = state.timeline
    .map((item) => item.kind === "assistant" ? `a${item.text.length}` : item.kind[0])
    .join("");
  const contentVersion = `${state.generation}:${visibleMessages.length}:${history.groups.length}:${timelineVersion}:${optimisticVersion}`;
  const empty = visibleMessages.length === 0
    && history.groups.length === 0
    && state.timeline.length === 0
    && optimisticUserMessages.length === 0
    && !state.running;

  useEffect(() => {
    if (!ownsSearchShortcut) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (!event.metaKey || event.ctrlKey || event.altKey || event.shiftKey || event.repeat) return;
      if (event.key.toLocaleLowerCase() !== "f") return;
      event.preventDefault();
      if (searchOpen) {
        rootRef.current?.querySelector("input")?.focus();
        return;
      }
      if (document.activeElement instanceof HTMLElement) {
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
    const sentJustNow = optimisticUserMessages.length > previousOptimisticCount.current;
    previousOptimisticCount.current = optimisticUserMessages.length;
    if (wasNearBottom.current || sentJustNow) {
      scroller.scrollTop = scroller.scrollHeight;
      wasNearBottom.current = true;
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

  const optimisticBlock = optimisticUserMessages.map((message) => (
    <Message
      key={`optimistic-${message.submissionId}`}
      id={`${instanceId}-optimistic-${message.submissionId}`}
      role="user"
      content={message.content}
      copyText={copyText}
    />
  ));

  return (
    <div ref={rootRef} className={styles.conversation}>
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
          {empty ? (
            <div className={styles.emptyState}>
              <p className={styles.emptyTitle}>Start a conversation</p>
              <p className={styles.emptyHint}>
                The agent works in your workspace and asks before anything
                sensitive. Press <kbd>&#8984;K</kbd> for commands and sessions.
              </p>
            </div>
          ) : null}
          {state.messages.map((message, messageIndex) => {
            const visible = ordinaryByIndex.get(messageIndex);
            const group = history.groupsByStartIndex.get(messageIndex);
            if (visible === undefined && group === undefined) return null;
            return (
              <div key={`flow-${messageIndex}`} className={styles.flowItem}>
                {visible === undefined ? null : (
                  <Message
                    id={`${instanceId}-message-${messageIndex}`}
                    role={visible.role}
                    content={visible.content}
                    copyText={copyText}
                  />
                )}
                {group === undefined ? null : groupActivities([...group.activities]).map((sub) => (
                  <ActivityCard
                    key={sub[0].activityId}
                    activities={sub}
                    openInspector={openInspector}
                  />
                ))}
              </div>
            );
          })}
          {optimisticBeforeTimeline ? optimisticBlock : null}
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
          {optimisticBeforeTimeline ? null : optimisticBlock}
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
