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

// The panel hangs below the chip, right-aligned and never wider than the frame,
// so it never pushes the header (or the widget's narrow panel) sideways.
const POPOVER =
  'absolute right-0 top-full z-30 mt-1 w-72 max-w-[calc(100vw-1.5rem)] whitespace-normal break-words rounded-md border border-border bg-background p-2 text-left text-[12px] shadow-md'
const QUIET = 'rounded-md px-2 py-0.5 text-muted-foreground hover:text-foreground disabled:opacity-50'

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

/** Whether a feature is actually working here: on in this session AND, when the
 *  session has an agent, granted to it. Anything short of that is off — there is
 *  no "waiting for you" state, because the session never asks (Jonathan,
 *  2026-10-19: the grant popup on every conversation was noise; the person turns
 *  it on themselves when they want it). */
export function memoryWorking(state: SessionMemory, f: MemoryFeature): boolean {
  return state[f].effective && (state[f].granted || !state.agent)
}

export interface SessionMemoryControl {
  state: SessionMemory
  editable: boolean
  manageUrl?: string
  features: MemoryFeature[]
  busy: MemoryFeature | 'keep' | null
  failed: boolean
  toggle(f: MemoryFeature): Promise<void>
  /** Keep the features allowed in this session for every conversation with this agent. */
  keep(): Promise<void>
}

/** Agent memory for one session — loaded once, `null` while hidden (not your
 *  session, memory not on for you, nothing available). Shared by the chip below
 *  and the chat page's ⋯ menu. */
export function useSessionMemory(
  sessionId: string | undefined,
  source?: SessionMemorySource,
): SessionMemoryControl | null {
  const [loaded, setLoaded] = useState<{
    state: SessionMemory
    editable: boolean
    manageUrl?: string
  } | null>(null)
  const [busy, setBusy] = useState<MemoryFeature | 'keep' | null>(null)
  const [failed, setFailed] = useState(false)

  useEffect(() => {
    let live = true
    setLoaded(null)
    if (!sessionId) return
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
  if (!sessionId || !loaded || !isMemory(loaded.state)) return null
  const { state } = loaded
  // Memory not on for this person yet (settings.HCP_PEOPLE): nothing to show.
  if (state.memory_enabled === false) return null
  const features = (['record', 'use'] as const).filter((f) => state[f].available)
  if (features.length === 0) return null
  const src = source ?? personSource(sessionId)

  const run = async (which: MemoryFeature | 'keep', act: () => Promise<SessionMemory>) => {
    setBusy(which)
    setFailed(false)
    try {
      const next = await act()
      setLoaded({ ...loaded, state: next })
    } catch {
      setFailed(true)
    } finally {
      setBusy(null)
    }
  }

  // Off → on is ONE click: switch it on for this session if it is not, then allow
  // this session's agent if it is not allowed yet. On → off only switches it off;
  // a session carries an override only when it really differs from the default.
  const toggle = (f: MemoryFeature) =>
    run(f, async () => {
      const cur = state[f]
      if (memoryWorking(state, f)) return src.save({ [f]: cur.default ? 'off' : 'inherit' })
      let next = state
      if (!cur.effective) next = await src.save({ [f]: cur.default ? 'inherit' : 'on' })
      if (next.agent && next[f].effective && !next[f].granted) next = await src.grant([f], 'session')
      return next
    })

  const keep = () =>
    run('keep', () =>
      src.grant(
        features.filter((f) => memoryWorking(state, f) && state[f].grant?.type === 'session'),
        'always',
      ),
    )

  return { ...loaded, features, busy, failed, toggle, keep }
}

/** Green when it is working here, red when it is not. */
export function MemoryDot({ on }: { on: boolean }) {
  return (
    <span
      aria-hidden
      className={`inline-block h-1.5 w-1.5 shrink-0 rounded-full ${on ? 'bg-success' : 'bg-destructive'}`}
    />
  )
}

/** "Learn ● Use ●" — one word and one dot per available feature. */
export function MemoryDots({ memory }: { memory: SessionMemoryControl }) {
  return (
    <span className="inline-flex items-center gap-2">
      {memory.features.map((f) => (
        <span key={f} className="inline-flex items-center gap-1">
          {LABELS[f].short}
          <MemoryDot on={memoryWorking(memory.state, f)} />
        </span>
      ))}
    </span>
  )
}

/** What the chip's dots say, for a screen reader and a test. */
export function memorySummary(memory: SessionMemoryControl): string {
  return memory.features
    .map((f) => `${LABELS[f].short} ${memoryWorking(memory.state, f) ? 'on' : 'off'}`)
    .join(', ')
}

/** The switches: what each feature does and whether it is working here. Changes
 *  are for this session only — never the person's defaults. Where the viewer may
 *  only SEE it (a site's widget acting for them), the state is shown with a link
 *  to change it in canopy. */
export function SessionMemoryPanel({ memory }: { memory: SessionMemoryControl }) {
  const { state, editable, manageUrl, features, busy, failed } = memory
  const name = state.agent?.name ?? 'this agent'
  const keepable =
    !!state.agent &&
    features.some((f) => memoryWorking(state, f) && state[f].grant?.type === 'session')
  return (
    <>
      <ul className="space-y-1.5">
        {features.map((f) => {
          const on = memoryWorking(state, f)
          return (
            <li key={f} className="flex items-start justify-between gap-2">
              <span className="min-w-0">
                <span className="flex items-center gap-1.5 text-foreground">
                  <MemoryDot on={on} />
                  {LABELS[f].name}
                </span>
                <span className="block text-muted-foreground">{on ? LABELS[f].on : LABELS[f].off}</span>
              </span>
              {editable ? (
                <button
                  type="button"
                  role="switch"
                  aria-checked={on}
                  aria-label={`${LABELS[f].name} in this conversation`}
                  disabled={busy !== null}
                  onClick={() => void memory.toggle(f)}
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
      {editable && keepable && (
        <button type="button" disabled={busy !== null} className={`mt-1.5 ${QUIET}`}
                onClick={() => void memory.keep()}>
          {busy === 'keep' ? '…' : `Allow ${name} in every conversation`}
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
    </>
  )
}

/** Agent memory for THIS session as a compact chip ("Learn ● Use ●") — for the
 *  embedded widget, which has no ⋯ menu to hold it. Shown only to the person
 *  whose session it is. It never opens on its own: a click opens the panel. */
export function SessionMemoryToggles({
  sessionId,
  source,
}: {
  sessionId: string
  source?: SessionMemorySource
}) {
  const memory = useSessionMemory(sessionId, source)
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

  if (!memory) return null

  return (
    <div ref={root} className="relative flex flex-col items-start">
      <button
        type="button"
        aria-haspopup="dialog"
        aria-expanded={open}
        aria-label={`Your memory in this conversation: ${memorySummary(memory)}`}
        title="Your memory in this conversation — click to see or change it"
        onClick={() => setOpen((o) => !o)}
        className="whitespace-nowrap rounded-md border border-border px-2 py-0.5 text-[12px] text-muted-foreground hover:bg-muted"
      >
        <MemoryDots memory={memory} />
      </button>
      {open && (
        <div role="dialog" aria-label="Your memory in this conversation" className={POPOVER}>
          <p className="mb-1 font-medium text-foreground">Your memory in this conversation</p>
          <SessionMemoryPanel memory={memory} />
        </div>
      )}
    </div>
  )
}
