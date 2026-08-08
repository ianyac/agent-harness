export function App() {
  return (
    <div className="app-shell">
      <nav aria-label="Sessions" className="session-sidebar" />
      <div className="conversation-shell">
        <header className="conversation-header">
          <h1>Agent Harness</h1>
        </header>
        <main aria-label="Conversation" className="transcript" />
      </div>
    </div>
  );
}
