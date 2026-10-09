import { useEffect, useMemo, useState } from 'react'

import { SessionMemoryToggles } from '@/components/chat/SessionMemoryToggles'

import { contactMemorySource, type ContactHcp, type ContactHcpState } from './sessionMemory'

/**
 * Agent memory for a CONTACT, on a site canopy trusts for their email
 * (apps/tokens/contact_hcp_api.py). Shown only when they are eligible: anywhere
 * else this renders nothing, exactly as before.
 *
 * - Not opted in: one quiet button, and on click one screen that says what it is
 *   before anything happens (HCP 4.1.6) — what canopy would learn, from whom, for
 *   how long, and how to see and take it back.
 * - Opted in: the chat page's own toggles and grant prompts, plus "What canopy
 *   knows", where they list and revoke grants, download everything, or opt out.
 */
const pretty = (c: string) => c.replace(/_/g, ' ')
const PANEL = 'mt-1 max-w-md rounded-md border border-border bg-background p-2 text-[12px]'
const PRIMARY = 'rounded-md border border-border px-2 py-0.5 hover:bg-muted disabled:opacity-50'
const QUIET = 'rounded-md px-2 py-0.5 text-muted-foreground hover:text-foreground disabled:opacity-50'

export function ContactMemory({
  hcp,
  sessionId,
  agentName,
}: {
  hcp: ContactHcp
  sessionId: string
  agentName: string
}) {
  const [state, setState] = useState<ContactHcpState | null>(null)
  const [view, setView] = useState<'none' | 'optin' | 'knows'>('none')
  const [use, setUse] = useState(false)
  const [busy, setBusy] = useState(false)
  const [failed, setFailed] = useState(false)
  const [epoch, setEpoch] = useState(0)

  useEffect(() => {
    let live = true
    hcp
      .state(sessionId)
      .then((s) => live && setState(s))
      .catch(() => live && setState(null))
    return () => {
      live = false
    }
  }, [hcp, sessionId, epoch])

  const source = useMemo(() => contactMemorySource(hcp, sessionId), [hcp, sessionId])

  if (!state || !state.eligible) return null

  const act = async (fn: () => Promise<ContactHcpState>, after: typeof view = 'none') => {
    setBusy(true)
    setFailed(false)
    try {
      setState(await fn())
      setView(after)
      setEpoch((e) => e + 1)
    } catch {
      setFailed(true)
    } finally {
      setBusy(false)
    }
  }

  const download = async () => {
    setBusy(true)
    try {
      const data = await hcp.exportAll()
      const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' }))
      const a = document.createElement('a')
      a.href = url
      a.download = 'what-canopy-knows-about-me.json'
      a.click()
      URL.revokeObjectURL(url)
    } catch {
      setFailed(true)
    } finally {
      setBusy(false)
    }
  }

  if (!state.opted_in) {
    if (view !== 'optin') {
      return (
        <button type="button" onClick={() => setView('optin')} className={QUIET}
                title="Let canopy remember things about your work across conversations">
          Remember me?
        </button>
      )
    }
    return (
      <div role="dialog" aria-label="Let canopy learn about you" className={PANEL}>
        <p className="text-foreground">
          Let <strong>{agentName}</strong> learn about your work in this conversation?
        </p>
        <p className="mt-0.5 text-muted-foreground">
          canopy would keep short facts about your work — {state.categories.map(pretty).join(', ')} —
          from what you say to {agentName}, under {state.email} (confirmed by {state.site}). For
          this conversation only, until it ends or for {state.session_grant_hours} hours; keeping
          it longer is a separate choice. You can see, download and take back everything here,
          any time. Only this conversation with {agentName} is used.
        </p>
        <label className="mt-1 flex items-center gap-1 text-muted-foreground">
          <input type="checkbox" checked={use} onChange={(e) => setUse(e.target.checked)} />
          Also let agents use what they've learned when you talk to them
        </label>
        <div className="mt-1.5 flex flex-wrap gap-1.5">
          <button type="button" disabled={busy} className={PRIMARY}
                  onClick={() => void act(() => hcp.optIn(sessionId, use))}>
            Allow for this conversation
          </button>
          <button type="button" disabled={busy} className={QUIET} onClick={() => setView('none')}>
            Not now
          </button>
        </div>
        {failed && <p className="mt-1 text-destructive">Not saved — try again.</p>}
      </div>
    )
  }

  return (
    <div className="flex flex-col items-start">
      <span className="flex items-center gap-1">
        <SessionMemoryToggles key={epoch} sessionId={sessionId} source={source} />
        <button type="button" className={QUIET}
                onClick={() => setView(view === 'knows' ? 'none' : 'knows')}>
          What canopy knows
        </button>
      </span>
      {view === 'knows' && (
        <div role="dialog" aria-label="What canopy knows about you" className={PANEL}>
          <p className="text-foreground">Held for {state.email}</p>
          {state.grants.length === 0 ? (
            <p className="mt-0.5 text-muted-foreground">No agent can learn about you right now.</p>
          ) : (
            <ul className="mt-0.5">
              {state.grants.map((g) => (
                <li key={g.grant_id} className="flex items-center justify-between gap-2">
                  <span className="text-muted-foreground">
                    {g.agent}: {g.features.map((f) => (f === 'record' ? 'learn' : 'use')).join(' + ')}
                    {g.type === 'temporary' ? ' · this conversation' : ' · always'}
                  </span>
                  <button type="button" disabled={busy} className={QUIET}
                          onClick={() => void act(() => hcp.revoke(g.grant_id), 'knows')}>
                    Take back
                  </button>
                </li>
              ))}
            </ul>
          )}
          {(state.entries ?? []).length > 0 && (
            <ul className="mt-1 border-t border-border pt-1">
              {(state.entries ?? []).map((e) => (
                <li key={e.entry_id} className="flex items-start justify-between gap-2">
                  <span className="text-foreground">{e.statement}</span>
                  <button type="button" disabled={busy} className={QUIET}
                          onClick={() => void act(() => hcp.removeEntry(e.entry_id), 'knows')}>
                    Remove
                  </button>
                </li>
              ))}
            </ul>
          )}
          <div className="mt-1.5 flex flex-wrap gap-1.5">
            <button type="button" disabled={busy} className={PRIMARY} onClick={() => void download()}>
              Download everything
            </button>
            <button type="button" disabled={busy} className={QUIET}
                    onClick={() => void act(() => hcp.policy({
                      record: { available: false, default: false },
                      use: { available: false, default: false },
                    }))}>
              Stop remembering me
            </button>
          </div>
          <p className="mt-1 text-muted-foreground">
            Stopping keeps what is already held — download it first if you want it — and no agent
            can learn or use it until you allow it again.
          </p>
          {failed && <p className="mt-1 text-destructive">Not saved — try again.</p>}
        </div>
      )}
    </div>
  )
}
