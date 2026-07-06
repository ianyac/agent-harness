import { useState } from 'react'
import type { TranscriptItem } from '../state/reducer'

export function InspectorPane({ item, systemPrompt }: {
  item: TranscriptItem | null
  systemPrompt: string
}) {
  const [tab, setTab] = useState<'message' | 'system'>('message')
  const message = item && 'message' in item ? item.message : undefined
  return (
    <aside className="inspector">
      <div className="tabs">
        <button onClick={() => setTab('message')} disabled={tab === 'message'}>message</button>
        <button onClick={() => setTab('system')} disabled={tab === 'system'}>system prompt</button>
      </div>
      {tab === 'system' ? (
        <pre>{systemPrompt}</pre>
      ) : item === null ? (
        <p>(select a transcript item)</p>
      ) : message === undefined ? (
        <p>(ephemeral — not part of the transcript state)</p>
      ) : (
        <pre>{JSON.stringify(message, null, 2)}</pre>
      )}
    </aside>
  )
}
