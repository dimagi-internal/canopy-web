import { useState, type JSX } from 'react'
import { Button, Input } from 'canopy-ui/ui'
import { setAgentDisplayName, type AgentDetailOut } from '@/api/agents'

/**
 * The agent's display name — optional, and changeable by its owner or an admin. Blank: it
 * is shown by its slug, which never changes. A utility agent (`labsdata`) may have none.
 */
export function AgentDisplayNameControl({ agent, canEdit }: {
  agent: AgentDetailOut
  canEdit: boolean
}): JSX.Element {
  const [value, setValue] = useState(agent.name)
  const [draft, setDraft] = useState(value)
  const [editing, setEditing] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  const save = async () => {
    setBusy(true)
    setError('')
    try {
      setValue((await setAgentDisplayName(agent.slug, draft.trim())).name)
      setEditing(false)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not save the display name')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="space-y-2 text-[13px]" data-testid="agent-display-name">
      {!editing ? (
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-foreground">{value || '—'}</span>
          {!value && <span className="text-[12px] text-muted-foreground">shown as <span className="font-mono">{agent.slug}</span></span>}
          {canEdit && (
            <Button size="sm" variant="outline" onClick={() => { setDraft(value); setEditing(true) }}
                    data-testid="agent-display-name-edit">
              {value ? 'Change' : 'Set one'}
            </Button>
          )}
        </div>
      ) : (
        <div className="flex flex-wrap items-center gap-2">
          <Input value={draft} onChange={(e) => setDraft(e.target.value)} className="w-full sm:w-72"
                 placeholder={agent.slug} aria-label="Display name" maxLength={120}
                 data-testid="agent-display-name-input" />
          <Button size="sm" disabled={busy} onClick={() => void save()} data-testid="agent-display-name-save">
            {busy ? 'Saving…' : 'Save'}
          </Button>
          <Button size="sm" variant="ghost" disabled={busy} onClick={() => setEditing(false)}>Cancel</Button>
        </div>
      )}
      {error && <p className="text-[12px] text-destructive">{error}</p>}
    </div>
  )
}
