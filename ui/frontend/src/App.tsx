import { useCallback, useEffect, useReducer, useRef, useState } from 'react'
import { Composer } from './components/Composer'
import { Header, type Meta } from './components/Header'
import { InspectorPane } from './components/InspectorPane'
import { PermissionPrompt } from './components/PermissionPrompt'
import { SessionSidebar, type SessionMeta } from './components/SessionSidebar'
import { Transcript } from './components/Transcript'
import { initialState, reducer } from './state/reducer'
import { SessionSocket, type SocketStatus } from './ws/client'

function wsUrl(sessionId: string): string {
  const scheme = location.protocol === 'https:' ? 'wss' : 'ws'
  return `${scheme}://${location.host}/api/sessions/${sessionId}/ws`
}

export default function App() {
  const [state, dispatch] = useReducer(reducer, initialState)
  const [sessions, setSessions] = useState<SessionMeta[]>([])
  const [activeId, setActiveId] = useState<string | null>(null)
  const [meta, setMeta] = useState<Meta | null>(null)
  const [socketStatus, setSocketStatus] = useState<SocketStatus>('closed')
  const [inspectorOpen, setInspectorOpen] = useState(false)
  const [selectedIndex, setSelectedIndex] = useState<number | null>(null)
  const [restoredInput, setRestoredInput] = useState('')
  const [errorDismissed, setErrorDismissed] = useState(false)
  const socketRef = useRef<SessionSocket | null>(null)
  const lastSentRef = useRef('')

  // a new error un-dismisses the banner
  useEffect(() => setErrorDismissed(false), [state.lastError])

  const refreshSessions = useCallback(async () => {
    setSessions(await (await fetch('/api/sessions')).json())
  }, [])

  useEffect(() => {
    refreshSessions()
    fetch('/api/meta').then(async (r) => setMeta(await r.json()))
  }, [refreshSessions])

  useEffect(() => {
    if (!activeId) return
    let cancelled = false
    dispatch({ type: 'reset' })
    setSelectedIndex(null)
    const socket = new SessionSocket(wsUrl(activeId), (event) => {
      if (cancelled) return
      dispatch(event)
      if (event.type === 'turn_error') setRestoredInput(lastSentRef.current)
      if (event.type === 'turn_done') refreshSessions()
    }, setSocketStatus)
    socketRef.current = socket
    socket.connect()
    return () => {
      cancelled = true
      socket.close()
    }
  }, [activeId, refreshSessions])

  const createSession = async () => {
    const created = await (await fetch('/api/sessions', { method: 'POST' })).json()
    await refreshSessions()
    setActiveId(created.id)
  }

  const send = (text: string) => {
    lastSentRef.current = text
    setRestoredInput('')
    socketRef.current?.send({ type: 'user_message', text })
    dispatch({ type: 'local_user_message', text })
  }

  const answer = (a: 'yes' | 'no' | 'always') => {
    const pending = state.pendingPermission
    if (!pending) return
    socketRef.current?.send({ type: 'permission_answer', id: pending.id, answer: a })
    dispatch({ type: 'local_permission_answer', id: pending.id, answer: a })
  }

  return (
    <div className={`app${inspectorOpen ? ' with-inspector' : ''}`}>
      <SessionSidebar
        sessions={sessions} activeId={activeId} mode={meta?.mode ?? '…'}
        onSelect={setActiveId} onCreate={createSession}
      />
      <div className="main">
        <Header
          meta={meta} socketStatus={socketStatus} inspectorOpen={inspectorOpen}
          onToggleInspector={() => setInspectorOpen((v) => !v)}
        />
        {activeId ? (
          <>
            <Transcript
              items={state.items} selectedIndex={selectedIndex} onSelect={setSelectedIndex}
            />
            {state.pendingPermission && (
              <PermissionPrompt
                name={state.pendingPermission.name}
                args={state.pendingPermission.args}
                answer={null} onAnswer={answer}
              />
            )}
            {state.lastError && !errorDismissed && (
              <div className="error-banner">
                turn failed: {state.lastError}
                <button onClick={() => setErrorDismissed(true)}>dismiss</button>
              </div>
            )}
            <Composer
              disabled={state.turnRunning || socketStatus !== 'open'}
              onSend={send}
              onCancel={() => socketRef.current?.send({ type: 'cancel' })}
              turnRunning={state.turnRunning}
              initialText={restoredInput}
            />
          </>
        ) : (
          <div className="transcript">
            <div className="notice">create or pick a session to start</div>
          </div>
        )}
      </div>
      {inspectorOpen && (
        <InspectorPane
          item={selectedIndex === null ? null : state.items[selectedIndex] ?? null}
          systemPrompt={meta?.system_prompt ?? ''}
        />
      )}
    </div>
  )
}
