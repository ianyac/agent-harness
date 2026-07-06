import type { Message, PermissionRequest, ServerEvent } from '../types/events'

export type TranscriptItem =
  | { kind: 'user'; text: string; message?: Message }
  | { kind: 'assistant'; text: string; streaming: boolean; message?: Message }
  | { kind: 'tool'; name: string; args: Record<string, unknown>;
      result: string | null; message?: Message }
  | { kind: 'permission'; id: string; name: string;
      args: Record<string, unknown>; answer: string | null; message?: undefined }
  | { kind: 'compaction'; summarized: number; message?: undefined }
  | { kind: 'notice'; text: string; message?: undefined }

export interface SessionState {
  items: TranscriptItem[]
  rawMessages: Message[]
  turnRunning: boolean
  pendingPermission: PermissionRequest | null
  turnStartIndex: number
  lastError: string | null
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
}

export function buildItemsFromMessages(messages: Message[]): TranscriptItem[] {
  const items: TranscriptItem[] = []
  const toolItemsByCallId = new Map<string, Extract<TranscriptItem, { kind: 'tool' }>>()
  for (const message of messages) {
    if (message.role === 'user' && typeof message.content === 'string') {
      items.push({ kind: 'user', text: message.content, message })
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
        }
        toolItemsByCallId.set(call.id, item)
        items.push(item)
      }
      if (typeof message.content === 'string' && message.content) {
        items.push({ kind: 'assistant', text: message.content, streaming: false, message })
      }
    } else if (message.role === 'tool') {
      const item = toolItemsByCallId.get(message.tool_call_id as string)
      if (item) item.result = String(message.content ?? '')
    }
  }
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

export function reducer(state: SessionState, action: Action): SessionState {
  switch (action.type) {
    case 'reset':
      return initialState
    case 'session_snapshot': {
      const items = buildItemsFromMessages(action.messages)
      if (action.streamed_text) {
        items.push({ kind: 'assistant', text: action.streamed_text, streaming: true })
      }
      if (action.pending_permission) {
        items.push({ ...action.pending_permission, kind: 'permission', answer: null })
      }
      return {
        items,
        rawMessages: action.messages,
        turnRunning: action.turn_running,
        pendingPermission: action.pending_permission,
        turnStartIndex: items.length,
        lastError: null,
      }
    }
    case 'local_user_message':
      return {
        ...state,
        turnStartIndex: state.items.length,
        items: [...state.items, { kind: 'user', text: action.text }],
        turnRunning: true,
        lastError: null,
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
        items: [...state.items, { kind: 'assistant', text: action.text, streaming: true }],
      }
    }
    case 'tool_call':
      return {
        ...state,
        items: [...closeStream(state.items),
          { kind: 'tool', name: action.name, args: action.args, result: null }],
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
            args: action.args, answer: null }],
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
          { kind: 'compaction', summarized: action.summarized }],
      }
    case 'turn_done': {
      // Rebuild this turn's message-backed items straight from the
      // authoritative transcript (the diff since the last turn_done),
      // rather than trying to patch the live, delta-built items in place —
      // that keeps assistant text/tool pairing correct even in degraded
      // mode, where no deltas/tool events streamed in before turn_done.
      // Ephemeral items (permission/compaction/notice) have no message
      // backing, so they're carried over as-is.
      const ephemeral = state.items
        .slice(state.turnStartIndex)
        .filter((item) =>
          item.kind === 'permission' || item.kind === 'compaction' || item.kind === 'notice')
      const newMessages = action.messages.slice(state.rawMessages.length)
      return {
        ...state,
        items: [
          ...state.items.slice(0, state.turnStartIndex),
          ...buildItemsFromMessages(newMessages),
          ...ephemeral,
        ],
        rawMessages: action.messages,
        turnRunning: false,
        pendingPermission: null,
      }
    }
    case 'turn_cancelled':
      return {
        ...state,
        items: [...state.items.slice(0, state.turnStartIndex),
          { kind: 'notice', text: 'turn cancelled' }],
        turnRunning: false,
        pendingPermission: null,
      }
    case 'turn_error':
      return {
        ...state,
        items: [...state.items.slice(0, state.turnStartIndex),
          { kind: 'notice', text: `turn failed: ${action.message}` }],
        turnRunning: false,
        pendingPermission: null,
        lastError: action.message,
      }
    default:
      return state
  }
}
