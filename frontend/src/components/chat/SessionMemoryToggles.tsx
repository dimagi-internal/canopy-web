import { useEffect, useState } from 'react'

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
  record: 'save what it learns about you',
  use: 'be told what has been learned about you',
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

const PROMPT = 'mt-1 max-w-md rounded-md border border-border bg-background p-2 text-[12px]'
const PRIMARY = 'rounded-md border border-border px-2 py-0.5 hover:bg-muted disabled:opacity-50'
const QUIET = 'rounded-md px-2 py-0.5 text-muted-foreground hover:text-foreground disabled:opacity-50'

/** The 4.1.6 disclosure and the authorizing act. Allowing is temporary — the
 *  default outcome (4.1.4); keeping it is asked separately, afterwards. */
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
    <div role="dialog" aria-label={`Grant ${name} access`} className={PROMPT}>
      <p className="text-foreground">
        Let <strong>{name}</strong> {asks}?
      </p>
      <p className="mt-0.5 text-muted-foreground">
        {(state.categories ?? []).map(pretty).join(', ')} · {actions} · for this session only,
        until {until(hours)} or when it ends. Only {name} — other agents ask separately.
      </p>
      <div className="mt-1.5 flex flex-wrap gap-1.5">
        <button type="button" disabled={busy} onClick={onAllow} className={PRIMARY}>
          Allow for this session
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
    <div role="dialog" aria-label={`Keep allowing ${name}`} className={PROMPT}>
      <p className="text-foreground">Keep allowing {name} after this session?</p>
      <p className="mt-0.5 text-muted-foreground">
        It would carry over to every session with {name} and stay in effect until you revoke it
        on your people page.
      </p>
      <div className="mt-1.5 flex flex-wrap gap-1.5">
        <button type="button" disabled={busy} onClick={onKeep} className={PRIMARY}>
          Keep allowing {name}
        </button>
        <button type="button" disabled={busy} onClick={onDismiss} className={QUIET}>
          Just this session
        </button>
      </div>
    </div>
  )
}

const LABELS: Record<MemoryFeature, { name: string; on: string; off: string }> = {
  record: {
    name: 'Learn',
    on: 'Agents in this session may record what they learn about you.',
    off: 'Agents in this session do not record anything about you.',
  },
  use: {
    name: 'Use',
    on: 'Agents in this session are told what has been learned about you.',
    off: 'Agents in this session are not told anything learned about you.',
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
 *  /people/me. A click sets this session's override; it never changes the
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

  return (
    <div className="flex flex-col items-start">
      <div className="flex items-center gap-1" aria-label="Agent memory in this session">
        {features.map((f) => {
          const on = state[f].effective
          const shown = on ? (state[f].granted ? 'on' : 'on, not granted') : 'off'
          const text = `${LABELS[f].name}: ${busy === f ? '…' : shown}`
          const look = on
            ? 'bg-primary/10 text-foreground'
            : 'border border-border text-muted-foreground'
          if (!editable) {
            return (
              <span
                key={f}
                title={`${on ? LABELS[f].on : LABELS[f].off} Change it in canopy.`}
                aria-label={`${LABELS[f].name} about me in this session: ${on ? 'on' : 'off'}`}
                className={`rounded-md px-2 py-0.5 text-[12px] ${look}`}
              >
                {text}
              </span>
            )
          }
          return (
            <button
              key={f}
              type="button"
              role="switch"
              aria-checked={on}
              title={`${on ? LABELS[f].on : LABELS[f].off} Click to turn ${on ? 'off' : 'on'} here.`}
              aria-label={`${LABELS[f].name} about me in this session`}
              disabled={busy !== null}
              onClick={() => void flip(f)}
              className={`rounded-md px-2 py-0.5 text-[12px] disabled:opacity-50 ${look} ${
                on ? '' : 'hover:bg-muted'
              }`}
            >
              {text}
            </button>
          )
        })}
        {!editable && manageUrl && (
          <a
            href={manageUrl}
            target="_blank"
            rel="noopener noreferrer"
            className="text-[12px] text-muted-foreground underline hover:text-foreground"
          >
            Change in canopy
          </a>
        )}
        {failed && <span className="text-[12px] text-destructive">not saved</span>}
      </div>
      {awaiting.length > 0 && editable && !dismissed && (
        <GrantPrompt
          state={state}
          features={awaiting}
          busy={busy !== null}
          onAllow={() => void grant(awaiting, 'session')}
          onDismiss={() => setDismissed(true)}
        />
      )}
      {allowed.length > 0 && editable && (
        <KeepPrompt
          name={name}
          busy={busy !== null}
          onKeep={() => void grant(allowed, 'always')}
          onDismiss={() => setAllowed([])}
        />
      )}
      {awaiting.length > 0 && !editable && (
        <span className="mt-0.5 text-[12px] text-muted-foreground">
          Not granted to {name} yet
          {manageUrl ? (
            <>
              {' — '}
              <a
                href={manageUrl}
                target="_blank"
                rel="noopener noreferrer"
                className="underline hover:text-foreground"
              >
                grant it in canopy
              </a>
            </>
          ) : null}
        </span>
      )}
    </div>
  )
}
