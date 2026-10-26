import { useState } from 'react'
import { holdAgent, releaseAgentHold, type AgentHold } from '@/api/agents'

// The agent hold (apps/agents Agent.held): while on, no runner starts the agent's
// turns — schedules, dispatches, mail, chats with it — and they wait queued for the
// release; every other agent runs as usual. Two faces over ONE state the agent
// shell owns: the banner above every tab (only while held, so nobody wonders why
// the agent is silent) and the Settings control that puts it on.

function since(iso: string | null | undefined): string {
  return iso ? new Date(iso).toLocaleString() : ''
}

function useHoldActions(slug: string, onChange: (h: AgentHold) => void) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const run = async (act: () => Promise<AgentHold>) => {
    if (busy) return
    setBusy(true)
    setError(null)
    try {
      onChange(await act())
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Failed to change the hold')
    } finally {
      setBusy(false)
    }
  }
  return {
    busy,
    error,
    hold: (note: string) => run(() => holdAgent(slug, note)),
    release: () => run(() => releaseAgentHold(slug)),
  }
}

export function AgentHoldBanner({
  slug,
  name,
  hold,
  canEdit,
  onChange,
}: {
  slug: string
  name: string
  hold: AgentHold
  canEdit: boolean
  onChange: (h: AgentHold) => void
}) {
  const { busy, error, release } = useHoldActions(slug, onChange)
  if (!hold.held) return null
  return (
    <div
      role="status"
      data-testid="agent-hold-banner"
      className="mx-6 mt-4 max-w-4xl rounded-lg border border-warning/30 bg-warning/10 px-4 py-3"
    >
      <div className="flex items-start justify-between gap-4">
        <div className="text-[13px] text-foreground">
          <p className="font-medium">{name} is on hold — no runner starts its turns.</p>
          <p className="mt-0.5 text-[12px] text-muted-foreground">
            {hold.note && <>“{hold.note}” · </>}
            {hold.held_by_email && <>by {hold.held_by_email} · </>}
            since {since(hold.held_at)} · {hold.queued} queued
          </p>
        </div>
        {canEdit && (
          <button
            type="button"
            disabled={busy}
            onClick={() => void release()}
            data-testid="agent-hold-release"
            className="shrink-0 rounded-md bg-primary px-3 py-1 text-[12px] font-medium text-primary-foreground disabled:opacity-60"
          >
            Release
          </button>
        )}
      </div>
      {error && <p className="mt-1 text-[11px] text-destructive">{error}</p>}
    </div>
  )
}

export function AgentHoldControl({
  slug,
  hold,
  canEdit,
  onChange,
}: {
  slug: string
  hold: AgentHold
  canEdit: boolean
  onChange: (h: AgentHold) => void
}) {
  const [note, setNote] = useState('')
  const actions = useHoldActions(slug, onChange)
  if (hold.held) {
    return (
      <div>
        <p className="text-[12px] text-foreground">
          On hold{hold.note && <> — “{hold.note}”</>}. {hold.queued} queued.
        </p>
        {canEdit && (
          <button
            type="button"
            disabled={actions.busy}
            onClick={() => void actions.release()}
            data-testid="agent-hold-control-release"
            className="mt-2 rounded-md bg-primary px-3 py-1 text-[12px] font-medium text-primary-foreground disabled:opacity-60"
          >
            Release
          </button>
        )}
        {actions.error && <p className="mt-1 text-[11px] text-destructive">{actions.error}</p>}
      </div>
    )
  }
  if (!canEdit) return <p className="text-[12px] text-muted-foreground">Running normally.</p>
  return (
    <div>
      <div className="flex flex-wrap items-center gap-2">
        <input
          value={note}
          onChange={(e) => setNote(e.target.value)}
          maxLength={500}
          placeholder="Why (shown to whoever finds it silent)"
          aria-label="Hold note"
          className="w-72 rounded-md border border-border bg-input px-2 py-1 text-[12px] text-foreground"
        />
        <button
          type="button"
          disabled={actions.busy}
          onClick={() => void actions.hold(note.trim())}
          data-testid="agent-hold-control-hold"
          className="rounded-md border border-border px-3 py-1 text-[12px] font-medium text-foreground hover:bg-muted disabled:opacity-60"
        >
          Put on hold
        </button>
      </div>
      {actions.error && <p className="mt-1 text-[11px] text-destructive">{actions.error}</p>}
    </div>
  )
}
