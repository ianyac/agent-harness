import { describe, expect, it } from 'vitest'
import type { Message } from '../types/events'
import {
  buildItemsFromMessages, initialState, reducer, type Action, type SessionState,
} from './reducer'

function play(actions: Action[], from: SessionState = initialState): SessionState {
  return actions.reduce(reducer, from)
}

const toolCallMessage: Message = {
  role: 'assistant', content: null,
  tool_calls: [{ id: 'call-1', type: 'function',
    function: { name: 'echo', arguments: '{"x": 1}' } }],
}
const toolResultMessage: Message = { role: 'tool', tool_call_id: 'call-1', content: 'echo:1' }

describe('buildItemsFromMessages', () => {
  it('maps user, assistant, and tool exchanges', () => {
    const items = buildItemsFromMessages([
      { role: 'user', content: 'hi' },
      toolCallMessage,
      toolResultMessage,
      { role: 'assistant', content: 'done' },
    ])
    expect(items.map((i) => i.kind)).toEqual(['user', 'tool', 'assistant'])
    expect(items[1]).toMatchObject({ name: 'echo', args: { x: 1 }, result: 'echo:1' })
    expect(items[2]).toMatchObject({ text: 'done', streaming: false })
    expect(items[2].kind === 'assistant' && items[2].message).toBeTruthy()
  })
})

describe('reducer', () => {
  it('snapshot rebuilds state including a mid-turn stream', () => {
    const state = play([{
      type: 'session_snapshot',
      messages: [{ role: 'user', content: 'q' }],
      turn_running: true,
      pending_permission: { id: 'perm-1', name: 'bash', args: { command: 'ls' } },
      streamed_text: 'partial ans',
    }])
    expect(state.turnRunning).toBe(true)
    expect(state.items.map((i) => i.kind)).toEqual(['user', 'assistant', 'permission'])
    expect(state.items[1]).toMatchObject({ text: 'partial ans', streaming: true })
    expect(state.pendingPermission?.id).toBe('perm-1')
  })

  it('live turn: user, deltas, tool events, done reconciliation', () => {
    const authoritative: Message[] = [
      { role: 'user', content: 'do it' },
      toolCallMessage,
      toolResultMessage,
      { role: 'assistant', content: 'all done' },
    ]
    const state = play([
      { type: 'local_user_message', text: 'do it' },
      { type: 'turn_started' },
      { type: 'tool_call', name: 'echo', args: { x: 1 } },
      { type: 'tool_result', name: 'echo', result: 'echo:1' },
      { type: 'text_delta', text: 'all ' },
      { type: 'text_delta', text: 'done' },
      { type: 'turn_done', messages: authoritative },
    ])
    expect(state.turnRunning).toBe(false)
    expect(state.items.map((i) => i.kind)).toEqual(['user', 'tool', 'assistant'])
    expect(state.items[2]).toMatchObject({ text: 'all done', streaming: false })
    expect(state.rawMessages).toEqual(authoritative)
    // reconciliation attached authoritative dicts to this turn's items
    expect(state.items[0].kind === 'user' && state.items[0].message).toEqual(authoritative[0])
    expect(state.items[2].kind === 'assistant' && state.items[2].message).toEqual(authoritative[3])
  })

  it('permission flow: request appends item, local answer records it', () => {
    const state = play([
      { type: 'local_user_message', text: 'go' },
      { type: 'permission_request', id: 'perm-1', name: 'bash', args: { command: 'rm' } },
      { type: 'local_permission_answer', id: 'perm-1', answer: 'no' },
    ])
    const perm = state.items.find((i) => i.kind === 'permission')
    expect(perm).toMatchObject({ id: 'perm-1', answer: 'no' })
    expect(state.pendingPermission).toBeNull()
  })

  it('degraded mode: whole text arrives only at turn_done', () => {
    const state = play([
      { type: 'local_user_message', text: 'hi' },
      { type: 'turn_started' },
      { type: 'turn_done', messages: [
        { role: 'user', content: 'hi' },
        { role: 'assistant', content: 'whole answer' },
      ]},
    ])
    expect(state.items.map((i) => i.kind)).toEqual(['user', 'assistant'])
    expect(state.items[1]).toMatchObject({ text: 'whole answer' })
  })

  it('cancel and error drop the turn items and leave a notice', () => {
    const base: Action[] = [
      { type: 'local_user_message', text: 'q1' },
      { type: 'turn_started' },
      { type: 'turn_done', messages: [
        { role: 'user', content: 'q1' }, { role: 'assistant', content: 'a1' },
      ]},
      { type: 'local_user_message', text: 'q2' },
      { type: 'turn_started' },
      { type: 'tool_call', name: 'echo', args: { x: 1 } },
    ]
    const cancelled = play([...base, { type: 'turn_cancelled' }])
    expect(cancelled.items.map((i) => i.kind)).toEqual(['user', 'assistant', 'notice'])
    const failed = play([...base, { type: 'turn_error', message: 'RuntimeError: boom' }])
    expect(failed.items.map((i) => i.kind)).toEqual(['user', 'assistant', 'notice'])
    expect(failed.lastError).toBe('RuntimeError: boom')
  })

  it('replay equals live for the message-backed items', () => {
    const authoritative: Message[] = [
      { role: 'user', content: 'do it' },
      toolCallMessage,
      toolResultMessage,
      { role: 'assistant', content: 'all done' },
    ]
    const live = play([
      { type: 'local_user_message', text: 'do it' },
      { type: 'turn_started' },
      { type: 'tool_call', name: 'echo', args: { x: 1 } },
      { type: 'tool_result', name: 'echo', result: 'echo:1' },
      { type: 'turn_done', messages: authoritative },
    ])
    const replayed = buildItemsFromMessages(authoritative)
    expect(live.items.map(({ message, ...rest }) => rest))
      .toEqual(replayed.map(({ message, ...rest }) => rest))
  })

  it('permission record survives turn_done in position', () => {
    const authoritative: Message[] = [
      { role: 'user', content: 'do it' },
      toolCallMessage,
      toolResultMessage,
      { role: 'assistant', content: 'all done' },
    ]
    const state = play([
      { type: 'local_user_message', text: 'do it' },
      { type: 'permission_request', id: 'perm-1', name: 'echo', args: { x: 1 } },
      { type: 'local_permission_answer', id: 'perm-1', answer: 'yes' },
      { type: 'tool_call', name: 'echo', args: { x: 1 } },
      { type: 'tool_result', name: 'echo', result: 'echo:1' },
      { type: 'turn_done', messages: authoritative },
    ])
    expect(state.items.map((i) => i.kind)).toEqual(['user', 'permission', 'tool', 'assistant'])
    const perm = state.items.find((i) => i.kind === 'permission')
    expect(perm).toMatchObject({ id: 'perm-1', answer: 'yes' })
  })

  it('compaction-shrunk turn_done preserves the divider without stale items', () => {
    const firstTurnMessages: Message[] = [
      { role: 'user', content: 'q1' },
      toolCallMessage,
      toolResultMessage,
      { role: 'assistant', content: 'a1' },
    ]
    const seeded = play([
      { type: 'local_user_message', text: 'q1' },
      { type: 'turn_started' },
      { type: 'tool_call', name: 'echo', args: { x: 1 } },
      { type: 'tool_result', name: 'echo', result: 'echo:1' },
      { type: 'turn_done', messages: firstTurnMessages },
    ])
    const postCompactionMessages: Message[] = [
      { role: 'assistant', content: 'summary of q1/a1' },
      { role: 'user', content: 'q2' },
      { role: 'assistant', content: 'a2' },
    ]
    const state = play([
      { type: 'local_user_message', text: 'q2' },
      { type: 'compaction', summarized: 3 },
      { type: 'turn_done', messages: postCompactionMessages },
    ], seeded)
    expect(postCompactionMessages.length).toBeLessThan(seeded.rawMessages.length)
    expect(state.items.map((i) => i.kind)).toEqual(['assistant', 'user', 'assistant', 'compaction'])
    expect(state.items[0]).toMatchObject({ text: 'summary of q1/a1' })
    expect(state.items[1]).toMatchObject({ text: 'q2' })
    expect(state.items[2]).toMatchObject({ text: 'a2' })
    expect(state.items[3]).toMatchObject({ summarized: 3 })
  })

  it('reconnect then finish: exactly one assistant item, no duplicate stub', () => {
    const userMsg: Message = { role: 'user', content: 'question' }
    const state = play([
      { type: 'session_snapshot', messages: [userMsg], turn_running: true,
        pending_permission: null, streamed_text: 'par' },
      { type: 'text_delta', text: 'tial' },
      { type: 'turn_done', messages: [userMsg, { role: 'assistant', content: 'partial answer' }] },
    ])
    const assistants = state.items.filter((i) => i.kind === 'assistant')
    expect(assistants).toHaveLength(1)
    expect(assistants[0]).toMatchObject({ text: 'partial answer', streaming: false })
  })
})
