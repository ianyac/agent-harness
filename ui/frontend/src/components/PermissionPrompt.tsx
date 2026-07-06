const LABELS: Record<string, string> = {
  yes: 'allowed once', no: 'denied', always: 'always allowed for this tool',
}

export function PermissionPrompt({ name, args, answer, onAnswer }: {
  name: string
  args: Record<string, unknown>
  answer: string | null
  onAnswer?: (a: 'yes' | 'no' | 'always') => void
}) {
  return (
    <div className="permission">
      <div>
        agent wants to run <strong>{name}</strong>
        <code> {JSON.stringify(args)}</code>
      </div>
      {answer ? (
        <div className="answered">{LABELS[answer] ?? answer}</div>
      ) : (
        <div className="buttons">
          <button onClick={() => onAnswer?.('yes')}>Yes</button>
          <button onClick={() => onAnswer?.('no')}>No</button>
          <button onClick={() => onAnswer?.('always')}>Always for this tool</button>
        </div>
      )}
    </div>
  )
}
