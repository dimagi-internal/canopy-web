import { useEffect, useState } from 'react'

import {
  getSessionMemory,
  setSessionMemory,
  type MemoryFeature,
  type SessionMemory,
} from '@/api/people'

const LABELS: Record<MemoryFeature, { name: string; on: string; off: string }> = {
  record: {
    name: 'Learn',
    on: 'Agents in this session may record what they learn about you. Click to turn off here.',
    off: 'Agents in this session do not record anything about you. Click to turn on here.',
  },
  use: {
    name: 'Use',
    on: 'Agents in this session are told what has been learned about you. Click to turn off here.',
    off: 'Agents in this session are not told anything learned about you. Click to turn on here.',
  },
}

/** Agent memory for THIS session — shown only to the person whose session it is
 *  (the route 404s for anyone else, and this renders nothing then), and only for
 *  the features they made available on /people/me. A click sets this session's
 *  override; it never changes the person's defaults. */
export function SessionMemoryToggles({ sessionId }: { sessionId: string }) {
  const [state, setState] = useState<SessionMemory | null>(null)
  const [busy, setBusy] = useState<MemoryFeature | null>(null)
  const [failed, setFailed] = useState(false)

  useEffect(() => {
    let live = true
    setState(null)
    getSessionMemory(sessionId)
      .then((s) => live && setState(s))
      .catch(() => live && setState(null))
    return () => {
      live = false
    }
  }, [sessionId])

  if (!state) return null
  const features = (['record', 'use'] as const).filter((f) => state[f].available)
  if (features.length === 0) return null

  const flip = async (f: MemoryFeature) => {
    const cur = state[f]
    // Back to the default when that gives what was asked for, so a session only
    // carries an override when it really differs.
    const want = !cur.effective
    const choice = want === cur.default ? 'inherit' : want ? 'on' : 'off'
    setBusy(f)
    setFailed(false)
    try {
      setState(await setSessionMemory(sessionId, { [f]: choice }))
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
        return (
          <button
            key={f}
            type="button"
            role="switch"
            aria-checked={on}
            title={on ? LABELS[f].on : LABELS[f].off}
            aria-label={`${LABELS[f].name} about me in this session`}
            disabled={busy !== null}
            onClick={() => void flip(f)}
            className={`rounded-md px-2 py-0.5 text-[12px] disabled:opacity-50 ${
              on
                ? 'bg-primary/10 text-foreground'
                : 'border border-border text-muted-foreground hover:bg-muted'
            }`}
          >
            {LABELS[f].name}: {busy === f ? '…' : on ? 'on' : 'off'}
          </button>
        )
      })}
      {failed && <span className="text-[12px] text-destructive">not saved</span>}
    </div>
  )
}
