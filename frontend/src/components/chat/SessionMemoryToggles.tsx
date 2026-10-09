import { useEffect, useState } from 'react'

import {
  getSessionMemory,
  setSessionMemory,
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
}

function personSource(sessionId: string): SessionMemorySource {
  return {
    load: async () => ({ state: await getSessionMemory(sessionId), editable: true }),
    save: (change) => setSessionMemory(sessionId, change),
  }
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
  const [busy, setBusy] = useState<MemoryFeature | null>(null)
  const [failed, setFailed] = useState(false)

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

  return (
    <div className="flex items-center gap-1" aria-label="Agent memory in this session">
      {features.map((f) => {
        const on = state[f].effective
        const text = `${LABELS[f].name}: ${busy === f ? '…' : on ? 'on' : 'off'}`
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
  )
}
