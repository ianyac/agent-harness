import type { Message, PermissionRequest, ServerEvent } from '../types/events'

export type TranscriptItem =
  | { kind: 'user'; text: string; message?: Message; key: string }
  | { kind: 'assistant'; text: string; streaming: boolean; message?: Message; key: string }
  | { kind: 'tool'; name: string; args: Record<string, unknown>;
      result: string | null; message?: Message; key: string }
  | { kind: 'permission'; id: string; name: string;
      args: Record<string, unknown>; answer: string | null; message?: undefined;
      anchor?: number; key: string }
  | { kind: 'compaction'; summarized: number; message?: undefined; anchor?: number; key: string }
  | { kind: 'notice'; text: string; message?: undefined; anchor?: number; key: string }

export interface SessionState {
  items: TranscriptItem[]
  rawMessages: Message[]
  turnRunning: boolean
  pendingPermission: PermissionRequest | null
  turnStartIndex: number
  lastError: string | null
  nextKey: number
}

export type Action =
  | ServerEvent
  | { type: 'local_user_message'; text: string }
  | { type: 'local_permission_answer'; id: string; answer: string }
  | { type: 'reset' }

export const initialState: SessionState = {
  items: [],
  rawMessages: [],
  turnRunning: false,
  pendingPermission: null,
  turnStartIndex: 0,
  lastError: null,
  nextKey: 0,
}

export function buildItemsFromMessages(messages: Message[]): TranscriptItem[] {
  const items: TranscriptItem[] = []
  const toolItemsByCallId = new Map<string, Extract<TranscriptItem, { kind: 'tool' }>>()
  messages.forEach((message, messageIndex) => {
    if (message.role === 'user' && typeof message.content === 'string') {
      items.push({ kind: 'user', text: message.content, message, key: `msg-${messageIndex}` })
    } else if (message.role === 'assistant') {
      const calls = (message.tool_calls ?? []) as Array<{
        id: string; function: { name: string; arguments: string }
      }>
      for (const call of calls) {
        const item: Extract<TranscriptItem, { kind: 'tool' }> = {
          kind: 'tool',
          name: call.function.name,
          args: safeParse(call.function.arguments),
          result: null,
          message,
          key: `msg-${messageIndex}-${call.id}`,
        }
        toolItemsByCallId.set(call.id, item)
        items.push(item)
      }
      if (typeof message.content === 'string' && message.content) {
        items.push({ kind: 'assistant', text: message.content, streaming: false, message,
          key: `msg-${messageIndex}` })
      }
    } else if (message.role === 'tool') {
      const item = toolItemsByCallId.get(message.tool_call_id as string)
      if (item) item.result = String(message.content ?? '')
    }
  })
  return items
}

function safeParse(raw: string): Record<string, unknown> {
  try {
    return JSON.parse(raw)
  } catch {
    return { raw }
  }
}

function closeStream(items: TranscriptItem[]): TranscriptItem[] {
  const last = items[items.length - 1]
  if (last?.kind === 'assistant' && last.streaming) {
    return [...items.slice(0, -1), { ...last, streaming: false }]
  }
  return items
}

// Number of message-backed items (user/assistant/tool) present in `items`.
// Used to anchor live-only items (permission/compaction/notice) to a position
// in the authoritative message list, since that list's length isn't a stable
// boundary once compaction can shrink it.
function messageBackedCount(items: TranscriptItem[]): number {
  return items.filter((item) =>
    item.kind === 'user' || item.kind === 'assistant' || item.kind === 'tool').length
}

type EphemeralItem = Extract<TranscriptItem, { kind: 'permission' | 'compaction' | 'notice' }>

function isEphemeral(item: TranscriptItem): item is EphemeralItem {
  return item.kind === 'permission' || item.kind === 'compaction' || item.kind === 'notice'
}

// Re-interleave live-only items into a freshly rebuilt message-backed list,
// using each live-only item's anchor (the message-backed count at the time
// it was appended) to place it back at the equivalent position. Items whose
// anchor is beyond the rebuilt list's length (e.g. a compaction divider
// whose messages were summarized away) land at the end.
function reinterleave(rebuilt: TranscriptItem[], ephemeral: EphemeralItem[]): TranscriptItem[] {
  const merged: TranscriptItem[] = []
  let ephemeralIndex = 0
  let count = 0
  for (const item of rebuilt) {
    while (ephemeralIndex < ephemeral.length
      && (ephemeral[ephemeralIndex].anchor ?? Infinity) <= count) {
      merged.push(ephemeral[ephemeralIndex])
      ephemeralIndex++
    }
    merged.push(item)
    count++
  }
  while (ephemeralIndex < ephemeral.length) {
    merged.push(ephemeral[ephemeralIndex])
    ephemeralIndex++
  }
  return merged
}

export function reducer(state: SessionState, action: Action): SessionState {
  switch (action.type) {
    case 'reset':
      return initialState
    case 'session_snapshot': {
      const items = buildItemsFromMessages(action.messages)
      let nextKey = 0
      if (action.streamed_text) {
        items.push({ kind: 'assistant', text: action.streamed_text, streaming: true,
          key: `live-${nextKey}` })
        nextKey += 1
      }
      if (action.pending_permission) {
        items.push({ ...action.pending_permission, kind: 'permission', answer: null,
          anchor: messageBackedCount(items), key: action.pending_permission.id })
      }
      return {
        items,
        rawMessages: action.messages,
        turnRunning: action.turn_running,
        pendingPermission: action.pending_permission,
        turnStartIndex: items.length,
        lastError: null,
        nextKey,
      }
    }
    case 'local_user_message':
      return {
        ...state,
        turnStartIndex: state.items.length,
        items: [...state.items, { kind: 'user', text: action.text, key: `live-${state.nextKey}` }],
        turnRunning: true,
        lastError: null,
        nextKey: state.nextKey + 1,
      }
    case 'turn_started':
      return { ...state, turnRunning: true }
    case 'text_delta': {
      const last = state.items[state.items.length - 1]
      if (last?.kind === 'assistant' && last.streaming) {
        const grown = { ...last, text: last.text + action.text }
        return { ...state, items: [...state.items.slice(0, -1), grown] }
      }
      return {
        ...state,
        items: [...state.items,
          { kind: 'assistant', text: action.text, streaming: true, key: `live-${state.nextKey}` }],
        nextKey: state.nextKey + 1,
      }
    }
    case 'tool_call':
      return {
        ...state,
        items: [...closeStream(state.items),
          { kind: 'tool', name: action.name, args: action.args, result: null,
            key: `live-${state.nextKey}` }],
        nextKey: state.nextKey + 1,
      }
    case 'tool_result': {
      const items = [...state.items]
      for (let i = items.length - 1; i >= 0; i--) {
        const item = items[i]
        if (item.kind === 'tool' && item.name === action.name && item.result === null) {
          items[i] = { ...item, result: action.result }
          break
        }
      }
      return { ...state, items }
    }
    case 'permission_request':
      return {
        ...state,
        items: [...closeStream(state.items),
          { kind: 'permission', id: action.id, name: action.name,
            args: action.args, answer: null, anchor: messageBackedCount(state.items),
            key: action.id }],
        pendingPermission: { id: action.id, name: action.name, args: action.args },
      }
    case 'local_permission_answer':
      return {
        ...state,
        items: state.items.map((item) =>
          item.kind === 'permission' && item.id === action.id
            ? { ...item, answer: action.answer }
            : item,
        ),
        pendingPermission: null,
      }
    case 'compaction':
      return {
        ...state,
        items: [...closeStream(state.items),
          { kind: 'compaction', summarized: action.summarized,
            anchor: messageBackedCount(state.items), key: `live-${state.nextKey}` }],
        nextKey: state.nextKey + 1,
      }
    case 'turn_done': {
      // The backend sends the FULL authoritative message list at turn_done
      // (not just this turn's diff), because mid-turn compaction can make it
      // shorter than rawMessages — a length-based boundary would be invalid.
      // So we always rebuild message-backed items from scratch, then
      // re-interleave every live-only item (permission/compaction/notice)
      // from the whole current item list back in at its anchored position.
      // A wholesale rebuild also means a stale streaming-assistant stub
      // (e.g. from a mid-turn reconnect) is never carried over — only the
      // finalized message from `action.messages` produces an assistant item.
      const rebuilt = buildItemsFromMessages(action.messages)
      const ephemeral = state.items.filter(isEphemeral)
      const merged = reinterleave(rebuilt, ephemeral)
      return {
        ...state,
        items: merged,
        rawMessages: action.messages,
        turnRunning: false,
        pendingPermission: null,
        turnStartIndex: merged.length,
      }
    }
    case 'turn_cancelled': {
      const prefix = state.items.slice(0, state.turnStartIndex)
      return {
        ...state,
        items: [...prefix,
          { kind: 'notice', text: 'turn cancelled', anchor: messageBackedCount(prefix),
            key: `live-${state.nextKey}` }],
        turnRunning: false,
        pendingPermission: null,
        nextKey: state.nextKey + 1,
      }
    }
    case 'turn_error': {
      const prefix = state.items.slice(0, state.turnStartIndex)
      return {
        ...state,
        items: [...prefix,
          { kind: 'notice', text: `turn failed: ${action.message}`,
            anchor: messageBackedCount(prefix), key: `live-${state.nextKey}` }],
        turnRunning: false,
        pendingPermission: null,
        lastError: action.message,
        nextKey: state.nextKey + 1,
      }
    }
    default:
      return state
  }
}
