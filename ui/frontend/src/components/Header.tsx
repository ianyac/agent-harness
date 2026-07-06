import type { SocketStatus } from '../ws/client'

export interface Meta { mode: string; workspace: string; system_prompt: string }

export function Header({ meta, socketStatus, inspectorOpen, onToggleInspector }: {
  meta: Meta | null
  socketStatus: SocketStatus
  inspectorOpen: boolean
  onToggleInspector: () => void
}) {
  return (
    <div className="header">
      <span>workspace: {meta?.workspace ?? '…'}</span>
      <span>mode: {meta?.mode ?? '…'}</span>
      <span className="spacer" />
      <span>{socketStatus}</span>
      <button onClick={onToggleInspector}>
        {inspectorOpen ? 'hide inspector' : 'inspector'}
      </button>
    </div>
  )
}
