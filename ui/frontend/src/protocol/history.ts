import type { ActivityItem, HarnessMessage, JsonObject, JsonValue } from "./types";

export type HistoryGroup = {
  readonly startIndex: number;
  readonly activities: readonly ActivityItem[];
};

export type MessageHistory = {
  readonly groups: readonly HistoryGroup[];
  readonly groupsByStartIndex: ReadonlyMap<number, HistoryGroup>;
  readonly byId: ReadonlyMap<string, ActivityItem>;
};

type ToolCall = {
  readonly callId: string | null;
  readonly name: string;
  readonly args: JsonObject;
};

function parsedArguments(raw: JsonValue | undefined): JsonObject {
  if (typeof raw !== "string") return {};
  try {
    const parsed: unknown = JSON.parse(raw);
    return parsed !== null && typeof parsed === "object" && !Array.isArray(parsed)
      ? parsed as JsonObject
      : {};
  } catch {
    return {};
  }
}

function toolCalls(message: HarnessMessage): ToolCall[] {
  if (message.role !== "assistant" || !Array.isArray(message.tool_calls)) return [];
  return message.tool_calls.flatMap((call) => {
    if (call === null || typeof call !== "object" || Array.isArray(call)) return [];
    const fn = call.function;
    if (fn === null || typeof fn !== "object" || Array.isArray(fn)) return [];
    if (typeof fn.name !== "string") return [];
    return [{
      callId: typeof call.id === "string" ? call.id : null,
      name: fn.name,
      args: parsedArguments(fn.arguments),
    }];
  });
}

function prose(message: HarnessMessage): boolean {
  if (message.role !== "user" && message.role !== "assistant") return false;
  const content = message.content;
  if (typeof content === "string") return content.trim() !== "";
  if (!Array.isArray(content)) return false;
  return content.some((part) =>
    typeof part === "string"
      ? part.trim() !== ""
      : part !== null && typeof part === "object" && !Array.isArray(part)
        && typeof part.text === "string" && part.text.trim() !== "",
  );
}

/**
 * Rebuild activity groups from the authoritative session messages so tool
 * work survives turn changes and reloads. Grouping mirrors the live rules:
 * consecutive tool steps form one group; user-facing prose breaks it.
 * Results attach by tool_call_id within the current group only, because
 * call ids are not unique across a whole session.
 */
export function messageHistory(messages: readonly HarnessMessage[]): MessageHistory {
  const groups: HistoryGroup[] = [];
  const byId = new Map<string, ActivityItem>();
  let current: { startIndex: number; activities: ActivityItem[] } | null = null;
  let pending = new Map<string, ActivityItem[]>();

  const commit = () => {
    if (current !== null && current.activities.length > 0) {
      groups.push({ startIndex: current.startIndex, activities: current.activities });
    }
    current = null;
    pending = new Map();
  };

  messages.forEach((message, messageIndex) => {
    if (message.role === "tool") {
      const callId = typeof message.tool_call_id === "string" ? message.tool_call_id : null;
      const waiting = callId === null ? undefined : pending.get(callId);
      const target = waiting?.shift();
      if (target !== undefined && message.content !== undefined) {
        target.result = message.content;
      }
      return;
    }
    if (prose(message)) commit();
    const calls = toolCalls(message);
    if (calls.length === 0) return;
    if (current === null) current = { startIndex: messageIndex, activities: [] };
    calls.forEach((call, position) => {
      const item: ActivityItem = {
        activityId: `history:${messageIndex}:${call.callId ?? position}`,
        turnId: "",
        parentActivityId: null,
        actor: call.name === "agent" ? "subagent" : "tool",
        name: call.name,
        args: call.args,
        startedAt: "",
        status: "complete",
      };
      current!.activities.push(item);
      byId.set(item.activityId, item);
      if (call.callId !== null) {
        const queue = pending.get(call.callId) ?? [];
        queue.push(item);
        pending.set(call.callId, queue);
      }
    });
  });
  commit();

  return {
    groups,
    groupsByStartIndex: new Map(groups.map((group) => [group.startIndex, group])),
    byId,
  };
}
