import { useState, type JSX } from 'react'
import { useNavigate } from 'react-router-dom'
import { Button, Input } from 'canopy-ui/ui'

import { deleteAgent } from '@/api/agents'

/** Delete an agent — what makes trying the New agent page safe: a test agent can
 *  be removed again. Typing the slug confirms it, because it cannot be undone and
 *  takes the agent's board, turns and schedules with it. The repo stays. */
export function DeleteAgentControl({ slug, name, workspace }: { slug: string; name: string; workspace: string }): JSX.Element {
  const navigate = useNavigate()
  const [typed, setTyped] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const remove = async () => {
    setBusy(true)
    setError('')
    try {
      await deleteAgent(slug)
      navigate(`/w/${workspace}/agents`)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
      setBusy(false)
    }
  }
  return (
    <div className="flex flex-col gap-2">
      <p className="text-[12px] text-muted-foreground">
        Removes {name} and its tasks, turns, schedules and settings from canopy. Its GitHub repository is not
        touched. Type <span className="font-mono text-foreground">{slug}</span> to confirm.
      </p>
      <div className="flex flex-wrap items-center gap-2">
        <Input
          aria-label="Type the agent's slug to confirm"
          value={typed}
          onChange={(e) => setTyped(e.target.value)}
          className="max-w-xs"
          placeholder={slug}
        />
        <Button variant="destructive" size="sm" disabled={typed !== slug || busy} onClick={() => void remove()}>
          {busy ? 'Deleting…' : `Delete ${name}`}
        </Button>
      </div>
      {error && <p className="text-[12px] text-destructive">{error}</p>}
    </div>
  )
}
