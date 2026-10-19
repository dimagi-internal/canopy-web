import { useState, type JSX } from 'react'
import { Button, Input } from 'canopy-ui/ui'
import { setAgentGoogleAccount, type AgentDetailOut } from '@/api/agents'

/**
 * The Google account the agent signs in to Drive, Docs and gog with, when it is not
 * its mailbox — e.g. scout@dimagi-ai.com beside a canopy-hosted scout@dimagi.ai.
 * Blank: the mailbox itself, when that is a Google mailbox (`Agent.google_identity`).
 */
export function AgentGoogleAccountControl({ agent, canEdit }: {
  agent: AgentDetailOut
  canEdit: boolean
}): JSX.Element {
  const [value, setValue] = useState(agent.google_account ?? '')
  const [draft, setDraft] = useState(value)
  const [editing, setEditing] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  const save = async () => {
    setBusy(true)
    setError('')
    try {
      setValue(await setAgentGoogleAccount(agent, draft.trim()))
      setEditing(false)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not save the Google account')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="space-y-2 text-[13px]" data-testid="agent-google-account">
      {!editing ? (
        <div className="flex flex-wrap items-center gap-2">
          <span className="font-mono text-foreground">{value || '—'}</span>
          {!value && <span className="text-[12px] text-muted-foreground">same as its mailbox, if that is Google</span>}
          {canEdit && (
            <Button size="sm" variant="outline" onClick={() => { setDraft(value); setEditing(true) }}
                    data-testid="agent-google-account-edit">
              {value ? 'Change' : 'Set one'}
            </Button>
          )}
        </div>
      ) : (
        <div className="flex flex-wrap items-center gap-2">
          <Input value={draft} onChange={(e) => setDraft(e.target.value)} className="w-full sm:w-72"
                 placeholder={`${agent.slug}@dimagi-ai.com`} aria-label="Google account"
                 data-testid="agent-google-account-input" />
          <Button size="sm" disabled={busy} onClick={() => void save()} data-testid="agent-google-account-save">
            {busy ? 'Saving…' : 'Save'}
          </Button>
          <Button size="sm" variant="ghost" disabled={busy} onClick={() => setEditing(false)}>Cancel</Button>
        </div>
      )}
      {error && <p className="text-[12px] text-destructive">{error}</p>}
    </div>
  )
}
