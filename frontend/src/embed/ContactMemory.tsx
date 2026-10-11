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
 * - Opted in: the chat page's own toggles and grant prompts, plus
 *   "Your memory", where they list and revoke grants, download everything, or opt out.
 * - Memory not offered to contacts yet (settings.HCP_CONTACT_OPT_IN): nothing — unless
 *   canopy already holds some of theirs, which stays theirs to see, download and delete.
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
  const enabled = state.memory_enabled !== false
  if (!enabled && !state.memory_paused) return null

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
      a.download = 'my-memory.json'
      a.click()
      URL.revokeObjectURL(url)
    } catch {
      setFailed(true)
    } finally {
      setBusy(false)
    }
  }

  if (enabled && !state.opted_in) {
    if (view !== 'optin') {
      return (
        <button type="button" onClick={() => setView('optin')} className={QUIET}
                title="Agents add what they learn to your memory, which you can see, remove or download">
          Let canopy learn from you?
        </button>
      )
    }
    return (
      <div role="dialog" aria-label="Let canopy learn from you" className={PANEL}>
        <p className="text-foreground">
          <strong>{agentName}</strong> would like to learn from you
        </p>
        <p className="mt-0.5 text-foreground">What it learns helps {agentName} help you next time.</p>
        <p className="mt-0.5 text-muted-foreground">
          canopy would start a memory for you: short facts about your work —{' '}
          {state.categories.map(pretty).join(', ')} — from what you say to {agentName}, under{' '}
          {state.email} (confirmed by {state.site}). For this conversation only, until it ends or
          for {state.session_grant_hours} hours; keeping it longer is a separate choice. You can
          see, remove or download your memory here, any time. Only this conversation with{' '}
          {agentName} is used.
        </p>
        <label className="mt-1 flex items-center gap-1 text-muted-foreground">
          <input type="checkbox" checked={use} onChange={(e) => setUse(e.target.checked)} />
          Also let agents use my memory when I talk to them
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
        {enabled && <SessionMemoryToggles key={epoch} sessionId={sessionId} source={source} />}
        <button type="button" className={QUIET}
                onClick={() => setView(view === 'knows' ? 'none' : 'knows')}>
          Your memory
        </button>
      </span>
      {view === 'knows' && (
        <div role="dialog" aria-label="Your memory" className={PANEL}>
          <p className="text-foreground">
            Your memory{enabled ? '' : ' (paused)'}, held for {state.email}
          </p>
          <p className="text-muted-foreground">
            {enabled
              ? 'See, remove or download it.'
              : 'Memory is paused here: no agent learns from you or uses it. You can still see, remove or download it.'}
          </p>
          {state.grants.length === 0 ? (
            <p className="mt-0.5 text-muted-foreground">No agent can learn from you or use your memory right now.</p>
          ) : (
            <ul className="mt-0.5">
              {state.grants.map((g) => (
                <li key={g.grant_id} className="flex items-center justify-between gap-2">
                  <span className="text-muted-foreground">
                    {g.agent}: {g.features.map((f) => (f === 'record' ? 'learn from me' : 'use my memory')).join(' + ')}
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
            {enabled && <button type="button" disabled={busy} className={QUIET}
                    onClick={() => void act(() => hcp.policy({
                      record: { available: false, default: false },
                      use: { available: false, default: false },
                    }))}>
              Stop my memory
            </button>}
          </div>
          {enabled && (
            <p className="mt-1 text-muted-foreground">
              Stopping keeps your memory as it is — download it first if you want it — and no agent
              can add to it or use it until you allow it again.
            </p>
          )}
          {failed && <p className="mt-1 text-destructive">Not saved — try again.</p>}
        </div>
      )}
    </div>
  )
}
