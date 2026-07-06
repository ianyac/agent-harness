import type { ClientMessage, ServerEvent } from '../types/events'

export type SocketStatus = 'connecting' | 'open' | 'closed'

const NO_RECONNECT_CODES = new Set([4000, 4404]) // superseded / unknown session

export class SessionSocket {
  private ws: WebSocket | null = null
  private attempts = 0
  private timer: ReturnType<typeof setTimeout> | null = null
  private closed = false

  constructor(
    private url: string,
    private onEvent: (event: ServerEvent) => void,
    private onStatus: (status: SocketStatus) => void,
    private wsFactory: (url: string) => WebSocket = (u) => new WebSocket(u),
  ) {}

  connect(): void {
    this.closed = false
    this.onStatus('connecting')
    const ws = this.wsFactory(this.url)
    this.ws = ws
    ws.onopen = () => {
      this.attempts = 0
      this.onStatus('open')
    }
    ws.onmessage = (e) => {
      try {
        this.onEvent(JSON.parse(e.data as string) as ServerEvent)
      } catch {
        // malformed server frame: ignore
      }
    }
    ws.onclose = (e) => {
      this.onStatus('closed')
      if (this.closed || NO_RECONNECT_CODES.has(e.code)) return
      const delay = Math.min(500 * 2 ** this.attempts, 10_000)
      this.attempts += 1
      this.timer = setTimeout(() => this.connect(), delay)
    }
  }

  close(): void {
    this.closed = true
    if (this.timer) clearTimeout(this.timer)
    this.ws?.close()
  }

  send(message: ClientMessage): void {
    if (this.ws && this.ws.readyState === 1 /* OPEN */) {
      this.ws.send(JSON.stringify(message))
    }
  }
}
