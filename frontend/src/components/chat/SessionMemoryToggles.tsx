import { useEffect, useRef, useState } from 'react'

import {
  getSessionMemory,
  grantSessionAgent,
  setSessionMemory,
  type GrantDuration,
  type MemoryFeature,
  type SessionMemory,
} from '@/api/people'

type Choice = 'on' | 'off' | 'inherit'

/** Where the toggles read and write. canopy's own pages use the person's routes;
 *  the embedded widget passes its own (its frame authenticates with a bearer
 *  token, not the app's session) — one component, two doors. */
export interface SessionMemorySource {
  /** The session's state, and whether THIS viewer may change it. `null` hides
   *  the toggles entirely (not your session, or not a person). */
  load(): Promise<{ state: SessionMemory; editable: boolean; manageUrl?: string } | null>
  save(change: Partial<Record<MemoryFeature, Choice>>): Promise<SessionMemory>
  /** The person's act: grant the session's agent these features (HCP 4.1.6). */
  grant(features: MemoryFeature[], duration: GrantDuration): Promise<SessionMemory>
}

function personSource(sessionId: string): SessionMemorySource {
  return {
    load: async () => ({ state: await getSessionMemory(sessionId), editable: true }),
    save: (change) => setSessionMemory(sessionId, change),
    grant: (features, duration) => grantSessionAgent(sessionId, features, duration, 'chat'),
  }
}

/** What a grant lets the agent do, in plain words — shown BEFORE the person acts. */
const ASKS: Record<MemoryFeature, string> = {
  record: 'learn from you',
  use: 'use your memory',
}

/** Why it matters, naming the organisation the agent serves — its reach is that
 *  organisation's memory, derived, never chosen here (HCP 4.1.5.1). */
function because(features: MemoryFeature[], org: string): string {
  const agents = org ? `${org}'s agents` : 'agents'
  if (features.includes('record') && features.includes('use'))
    return `It can use what ${agents} have learned, and what it learns helps them help you next time.`
  if (features.includes('record')) return `What it learns helps ${agents} help you next time.`
  return `It can use what ${agents} have learned about you.`
}
const ACTIONS: Record<MemoryFeature, string> = { record: 'write', use: 'read' }

const pretty = (category: string) => category.replace(/_/g, ' ')

const until = (hours: number) =>
  new Date(Date.now() + hours * 3600_000).toLocaleTimeString([], {
    hour: 'numeric',
    minute: '2-digit',
  })

/** The features on in this session that this agent has not been granted: what
 *  the session asks the person about (the agent never asks). */
function awaitingGrant(state: SessionMemory): MemoryFeature[] {
  return (['record', 'use'] as const).filter(
    (f) => state[f].available && state[f].effective && !state[f].granted,
  )
}

// Popovers hang below the chip, right-aligned and never wider than the frame,
// so they never push the header (or the widget's narrow panel) sideways.
const POPOVER =
  'absolute right-0 top-full z-30 mt-1 w-72 max-w-[calc(100vw-1.5rem)] whitespace-normal break-words rounded-md border border-border bg-background p-2 text-left text-[12px] shadow-md'
const PRIMARY = 'rounded-md border border-border px-2 py-0.5 hover:bg-muted disabled:opacity-50'
const QUIET = 'rounded-md px-2 py-0.5 text-muted-foreground hover:text-foreground disabled:opacity-50'

/** The 4.1.6 disclosure and the authorizing act, kept to two lines: who, what
 *  (categories + action) and for how long. Allowing is temporary — the default
 *  outcome (4.1.4); keeping it is asked separately, afterwards. */
function GrantPrompt({
  state,
  features,
  busy,
  onAllow,
  onDismiss,
}: {
  state: SessionMemory
  features: MemoryFeature[]
  busy: boolean
  onAllow: () => void
  onDismiss: () => void
}) {
  const name = state.agent?.name ?? 'This agent'
  const asks = features.map((f) => ASKS[f]).join(' and ')
  const actions = features.map((f) => ACTIONS[f]).join(' and ')
  const hours = state.session_grant_hours ?? 24
  return (
    <div role="dialog" aria-label={`Grant ${name} access`} className={POPOVER}>
      <p className="text-foreground">
        <strong>{name}</strong> would like to {asks}
      </p>
      <p className="mt-0.5 text-muted-foreground" title={because(features, state.org ?? '')}>
        {(state.categories ?? []).map(pretty).join(', ')} · {actions} · for this conversation only,
        until {until(hours)}. Only {name}.
      </p>
      <div className="mt-1.5 flex flex-wrap gap-1.5">
        <button type="button" disabled={busy} onClick={onAllow} className={PRIMARY}>
          Allow for this conversation
        </button>
        <button type="button" disabled={busy} onClick={onDismiss} className={QUIET}>
          Not now
        </button>
      </div>
    </div>
  )
}

/** The separate act that elects persistence (4.1.4): asked only after allowing,
 *  says what persistent means first, and nothing is preselected. */
function KeepPrompt({
  name,
  busy,
  onKeep,
  onDismiss,
}: {
  name: string
  busy: boolean
  onKeep: () => void
  onDismiss: () => void
}) {
  return (
    <div role="dialog" aria-label={`Keep allowing ${name}`} className={POPOVER}>
      <p className="text-foreground">Keep allowing {name} after this conversation?</p>
      <p className="mt-0.5 text-muted-foreground">
        Every conversation with {name}, until you turn it off in Your memory.
      </p>
      <div className="mt-1.5 flex flex-wrap gap-1.5">
        <button type="button" disabled={busy} onClick={onKeep} className={PRIMARY}>
          Keep allowing until I turn it off
        </button>
        <button type="button" disabled={busy} onClick={onDismiss} className={QUIET}>
          Just this conversation
        </button>
      </div>
    </div>
  )
}

const LABELS: Record<MemoryFeature, { short: string; name: string; on: string; off: string }> = {
  record: {
    short: 'Learn',
    name: 'Learn from me',
    on: 'Agents can add what they learn to your memory in this conversation.',
    off: 'Agents add nothing to your memory in this conversation.',
  },
  use: {
    short: 'Use',
    name: 'Use my memory',
    on: 'Agents can read your memory to help you in this conversation.',
    off: 'Agents do not read your memory in this conversation.',
  },
}

function isMemory(s: unknown): s is SessionMemory {
  const m = s as Partial<SessionMemory> | null
  return (
    !!m &&
    typeof m.record?.available === 'boolean' &&
    typeof m.use?.available === 'boolean'
  )
}

/** Agent memory for THIS session — shown only to the person whose session it is
 *  (anyone else gets nothing), and only for the features they made available on
 *  /people/me. One compact chip ("Learn ✓ · Use ✓"); a click opens a panel that
 *  says what each does and switches it for this session only — never the
 *  person's defaults. Where the viewer may only SEE it (a site's widget acting
 *  for them), the state is shown with a link to change it in canopy. */
export function SessionMemoryToggles({
  sessionId,
  source,
}: {
  sessionId: string
  source?: SessionMemorySource
}) {
  const [loaded, setLoaded] = useState<{
    state: SessionMemory
    editable: boolean
    manageUrl?: string
  } | null>(null)
  const [busy, setBusy] = useState<MemoryFeature | 'grant' | null>(null)
  const [failed, setFailed] = useState(false)
  const [dismissed, setDismissed] = useState(false)
  /** The features just allowed for this session — the next question is whether to keep them. */
  const [allowed, setAllowed] = useState<MemoryFeature[]>([])
  const [open, setOpen] = useState(false)
  const root = useRef<HTMLDivElement>(null)

  // The panel closes on a click outside it or Escape, like any popover.
  useEffect(() => {
    if (!open) return
    const away = (e: MouseEvent) => {
      if (root.current && !root.current.contains(e.target as Node)) setOpen(false)
    }
    const esc = (e: KeyboardEvent) => e.key === 'Escape' && setOpen(false)
    document.addEventListener('mousedown', away)
    document.addEventListener('keydown', esc)
    return () => {
      document.removeEventListener('mousedown', away)
      document.removeEventListener('keydown', esc)
    }
  }, [open])

  useEffect(() => {
    let live = true
    setLoaded(null)
    ;(source ?? personSource(sessionId))
      .load()
      .then((l) => live && setLoaded(l))
      .catch(() => live && setLoaded(null))
    return () => {
      live = false
    }
    // `source` is built per render by callers; the session is what identifies it.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionId])

  // A shape this does not recognise hides the toggles rather than taking the
  // whole panel down with it (the widget mounts this inside its chat header).
  if (!loaded || !isMemory(loaded.state)) return null
  const { state, editable, manageUrl } = loaded
  // Memory not on for this person yet (settings.HCP_PEOPLE): no chips, no prompts.
  if (state.memory_enabled === false) return null
  const features = (['record', 'use'] as const).filter((f) => state[f].available)
  if (features.length === 0) return null

  const flip = async (f: MemoryFeature) => {
    const cur = state[f]
    // Back to the default when that gives what was asked for, so a session only
    // carries an override when it really differs.
    const want = !cur.effective
    const choice: Choice = want === cur.default ? 'inherit' : want ? 'on' : 'off'
    setBusy(f)
    setFailed(false)
    try {
      const next = await (source ?? personSource(sessionId)).save({ [f]: choice })
      setLoaded({ ...loaded, state: next })
    } catch {
      setFailed(true)
    } finally {
      setBusy(null)
    }
  }

  const awaiting = state.agent ? awaitingGrant(state) : []
  const name = state.agent?.name ?? 'this agent'

  const grant = async (which: MemoryFeature[], duration: GrantDuration) => {
    setBusy('grant')
    setFailed(false)
    try {
      const next = await (source ?? personSource(sessionId)).grant(which, duration)
      setLoaded({ ...loaded, state: next })
      setAllowed(duration === 'session' ? which : [])
    } catch {
      setFailed(true)
    } finally {
      setBusy(null)
    }
  }

  // The chip: one word per feature, its mark saying whether it is working here —
  // ✓ on and allowed, ? on but waiting for the person's OK, – off.
  const mark = (f: MemoryFeature) =>
    !state[f].effective ? '–' : state[f].granted || !state.agent ? '✓' : '?'
  const chip = features.map((f) => `${LABELS[f].short} ${mark(f)}`).join(' · ')
  const grantText = (f: MemoryFeature) => {
    const g = state[f].grant
    if (!state[f].effective) return 'off here'
    if (!state.agent) return 'on'
    if (!state[f].granted) return `on — ${name} not allowed yet`
    return g?.type === 'persistent' ? `on — ${name} always allowed` : `on — ${name} allowed this conversation`
  }

  return (
    <div ref={root} className="relative flex flex-col items-start">
      <button
        type="button"
        aria-haspopup="dialog"
        aria-expanded={open}
        aria-label="Your memory in this conversation"
        title="Your memory in this conversation — click to see or change it"
        onClick={() => setOpen((o) => !o)}
        className={`whitespace-nowrap rounded-md px-2 py-0.5 text-[12px] hover:bg-muted ${
          features.some((f) => state[f].effective)
            ? 'bg-primary/10 text-foreground'
            : 'border border-border text-muted-foreground'
        }`}
      >
        {chip}
      </button>
      {open && (
        <div role="dialog" aria-label="Your memory in this conversation" className={POPOVER}>
          <p className="font-medium text-foreground">Your memory in this conversation</p>
          <ul className="mt-1 space-y-1.5">
            {features.map((f) => {
              const on = state[f].effective
              return (
                <li key={f} className="flex items-start justify-between gap-2">
                  <span className="min-w-0">
                    <span className="text-foreground">{LABELS[f].name}</span>
                    <span className="block text-muted-foreground">{on ? LABELS[f].on : LABELS[f].off}</span>
                    {state.agent && <span className="block text-muted-foreground">{grantText(f)}</span>}
                  </span>
                  {editable ? (
                    <button
                      type="button"
                      role="switch"
                      aria-checked={on}
                      aria-label={`${LABELS[f].name} in this conversation`}
                      disabled={busy !== null}
                      onClick={() => void flip(f)}
                      className={`shrink-0 rounded-md px-2 py-0.5 disabled:opacity-50 ${
                        on ? 'bg-primary/10 text-foreground' : 'border border-border text-muted-foreground hover:bg-muted'
                      }`}
                    >
                      {busy === f ? '…' : on ? 'On' : 'Off'}
                    </button>
                  ) : (
                    <span className="shrink-0 text-muted-foreground">{on ? 'On' : 'Off'}</span>
                  )}
                </li>
              )
            })}
          </ul>
          {editable && awaiting.length > 0 && (
            <button type="button" disabled={busy !== null} className={`mt-1.5 ${PRIMARY}`}
                    onClick={() => void grant(awaiting, 'session')}>
              Allow {name} for this conversation
            </button>
          )}
          {failed && <p className="mt-1 text-destructive">Not saved — try again.</p>}
          <p className="mt-1.5 border-t border-border pt-1 text-muted-foreground">
            {editable ? (
              <>
                Changes here are for this conversation only.{' '}
                <a href={manageUrl ?? '/people/me'} target="_blank" rel="noopener noreferrer"
                   className="underline hover:text-foreground">
                  Your defaults and agents
                </a>
              </>
            ) : manageUrl ? (
              <a href={manageUrl} target="_blank" rel="noopener noreferrer"
                 className="underline hover:text-foreground">
                Change in canopy
              </a>
            ) : null}
          </p>
        </div>
      )}
      {!open && awaiting.length > 0 && editable && !dismissed && (
        <GrantPrompt
          state={state}
          features={awaiting}
          busy={busy !== null}
          onAllow={() => void grant(awaiting, 'session')}
          onDismiss={() => setDismissed(true)}
        />
      )}
      {!open && allowed.length > 0 && editable && (
        <KeepPrompt
          name={name}
          busy={busy !== null}
          onKeep={() => void grant(allowed, 'always')}
          onDismiss={() => setAllowed([])}
        />
      )}
    </div>
  )
}
