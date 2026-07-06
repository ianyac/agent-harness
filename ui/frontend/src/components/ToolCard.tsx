import type { TranscriptItem } from '../state/reducer'

export function ToolCard({ item, selected, onSelect }: {
  item: Extract<TranscriptItem, { kind: 'tool' }>
  selected: boolean
  onSelect: () => void
}) {
  const summary = `${item.name}(${JSON.stringify(item.args)})`
  return (
    <details className={`tool-card${selected ? ' selected' : ''}`} onClick={onSelect}>
      <summary>
        ⚙ {summary.length > 120 ? summary.slice(0, 117) + '…' : summary}
        {item.result === null && <span className="pending"> · running…</span>}
      </summary>
      <pre>{JSON.stringify(item.args, null, 2)}</pre>
      {item.result !== null && <pre>{item.result}</pre>}
    </details>
  )
}
