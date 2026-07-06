import type { TranscriptItem } from '../state/reducer'
import { PermissionPrompt } from './PermissionPrompt'
import { ToolCard } from './ToolCard'

export function Transcript({ items, selectedIndex, onSelect }: {
  items: TranscriptItem[]
  selectedIndex: number | null
  onSelect: (index: number) => void
}) {
  return (
    <div className="transcript">
      {items.map((item, index) => {
        const selected = index === selectedIndex
        const select = () => onSelect(index)
        switch (item.kind) {
          case 'user':
          case 'assistant': {
            const classes = ['bubble', item.kind]
            if (selected) classes.push('selected')
            if (item.kind === 'assistant' && item.streaming) classes.push('streaming-cursor')
            return (
              <div key={index} className={classes.join(' ')} onClick={select}>
                {item.text}
              </div>
            )
          }
          case 'tool':
            return <ToolCard key={index} item={item} selected={selected} onSelect={select} />
          case 'permission':
            // answered record only; the live prompt (with buttons) is App's
            return (
              <PermissionPrompt key={index} name={item.name}
                args={item.args} answer={item.answer ?? '(pending)'} />
            )
          case 'compaction':
            return (
              <div key={index} className="compaction-divider">
                {item.summarized} messages compacted into a summary
              </div>
            )
          case 'notice':
            return <div key={index} className="notice">{item.text}</div>
        }
      })}
    </div>
  )
}
