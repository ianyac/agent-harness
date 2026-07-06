import { useEffect, useState } from 'react'

export function Composer({ disabled, onSend, onCancel, turnRunning, initialText }: {
  disabled: boolean
  onSend: (text: string) => void
  onCancel: () => void
  turnRunning: boolean
  initialText: string
}) {
  const [text, setText] = useState(initialText)
  useEffect(() => setText(initialText), [initialText])

  const send = () => {
    const trimmed = text.trim()
    if (!trimmed || disabled) return
    onSend(trimmed)
    setText('')
  }

  return (
    <div className="composer">
      <textarea
        value={text}
        placeholder="Message the agent…"
        onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault()
            send()
          }
        }}
      />
      {turnRunning
        ? <button onClick={onCancel}>Cancel</button>
        : <button onClick={send} disabled={disabled}>Send</button>}
    </div>
  )
}
