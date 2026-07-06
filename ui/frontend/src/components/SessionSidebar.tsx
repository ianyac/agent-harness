export interface SessionMeta { id: string; created_at: number; updated_at: number }

export function SessionSidebar({ sessions, activeId, mode, onSelect, onCreate }: {
  sessions: SessionMeta[]
  activeId: string | null
  mode: string
  onSelect: (id: string) => void
  onCreate: () => void
}) {
  return (
    <nav className="sidebar">
      <button className="new-session" onClick={onCreate}>+ new session</button>
      {sessions.map((s) => (
        <button
          key={s.id}
          className={`session${s.id === activeId ? ' active' : ''}`}
          onClick={() => onSelect(s.id)}
        >
          {s.id} · {new Date(s.updated_at * 1000).toLocaleTimeString()}
        </button>
      ))}
      <div className="mode">mode: {mode}</div>
    </nav>
  )
}
