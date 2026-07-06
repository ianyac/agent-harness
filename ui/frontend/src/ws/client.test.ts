import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { SessionSocket, type SocketStatus } from './client'

class FakeWebSocket {
  static instances: FakeWebSocket[] = []
  onopen: (() => void) | null = null
  onclose: ((e: { code: number }) => void) | null = null
  onmessage: ((e: { data: string }) => void) | null = null
  sent: string[] = []
  readyState = 0 // CONNECTING
  constructor(public url: string) {
    FakeWebSocket.instances.push(this)
  }
  send(data: string) { this.sent.push(data) }
  close() { this.readyState = 3 }
  open() { this.readyState = 1; this.onopen?.() }
  serverClose(code: number) { this.readyState = 3; this.onclose?.({ code }) }
  message(payload: unknown) { this.onmessage?.({ data: JSON.stringify(payload) }) }
}

describe('SessionSocket', () => {
  let events: unknown[]
  let statuses: SocketStatus[]
  let socket: SessionSocket

  beforeEach(() => {
    vi.useFakeTimers()
    FakeWebSocket.instances = []
    events = []
    statuses = []
    socket = new SessionSocket(
      'ws://test/api/sessions/s1/ws',
      (e) => events.push(e),
      (s) => statuses.push(s),
      (url) => new FakeWebSocket(url) as unknown as WebSocket,
    )
  })
  afterEach(() => vi.useRealTimers())

  it('delivers parsed events and sends only when open', () => {
    socket.connect()
    const ws = FakeWebSocket.instances[0]
    socket.send({ type: 'user_message', text: 'dropped' })
    expect(ws.sent).toEqual([])
    ws.open()
    socket.send({ type: 'user_message', text: 'hi' })
    expect(JSON.parse(ws.sent[0])).toEqual({ type: 'user_message', text: 'hi' })
    ws.message({ type: 'turn_started' })
    expect(events).toEqual([{ type: 'turn_started' }])
    expect(statuses).toEqual(['connecting', 'open'])
  })

  it('reconnects with backoff on unexpected close', () => {
    socket.connect()
    FakeWebSocket.instances[0].open()
    FakeWebSocket.instances[0].serverClose(1006)
    expect(statuses.at(-1)).toBe('closed')
    vi.advanceTimersByTime(500)
    expect(FakeWebSocket.instances).toHaveLength(2)
    FakeWebSocket.instances[1].serverClose(1006)
    vi.advanceTimersByTime(999)
    expect(FakeWebSocket.instances).toHaveLength(2) // backoff doubled
    vi.advanceTimersByTime(1)
    expect(FakeWebSocket.instances).toHaveLength(3)
  })

  it('does not reconnect after intentional close or supersede codes', () => {
    socket.connect()
    FakeWebSocket.instances[0].open()
    socket.close()
    vi.advanceTimersByTime(60_000)
    expect(FakeWebSocket.instances).toHaveLength(1)

    socket.connect()
    FakeWebSocket.instances[1].open()
    FakeWebSocket.instances[1].serverClose(4000)
    vi.advanceTimersByTime(60_000)
    expect(FakeWebSocket.instances).toHaveLength(2)
  })
})
