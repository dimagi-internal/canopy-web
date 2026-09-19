import { useEffect, useState, type JSX } from 'react'
import { apiV2 } from '@/api/client.v2'

/**
 * "Tell me a chat is done once it has been quiet for N minutes." Waiting, rather
 * than pushing on every finished turn, is what keeps an agent's back-to-back
 * turns from buzzing once each. 0 = never; a chat's own "every reply" toggle
 * still fires.
 */
export function QuietMinutes(): JSX.Element | null {
  const [minutes, setMinutes] = useState<number | null>(null)
  const [draft, setDraft] = useState('')
  const [saved, setSaved] = useState(false)

  useEffect(() => {
    let cancelled = false
    void apiV2.GET('/api/push/preferences').then(({ data }) => {
      if (cancelled || !data) return
      setMinutes(data.session_idle_minutes)
      setDraft(String(data.session_idle_minutes))
    })
    return () => {
      cancelled = true
    }
  }, [])

  if (minutes === null) return null

  const save = async () => {
    const n = Math.round(Number(draft))
    if (!Number.isFinite(n) || n < 0 || n > 1440 || n === minutes) {
      setDraft(String(minutes))
      return
    }
    const { data } = await apiV2.PATCH('/api/push/preferences', {
      body: { session_idle_minutes: n },
    })
    if (data) {
      setMinutes(data.session_idle_minutes)
      setDraft(String(data.session_idle_minutes))
      setSaved(true)
      window.setTimeout(() => setSaved(false), 1500)
    }
  }

  return (
    <label className="flex items-center gap-2 text-[12px] text-muted-foreground">
      Tell me a chat is done after
      <input
        type="number"
        min={0}
        max={1440}
        inputMode="numeric"
        data-testid="quiet-minutes"
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        onBlur={() => void save()}
        onKeyDown={(e) => {
          if (e.key === 'Enter') (e.target as HTMLInputElement).blur()
        }}
        className="w-14 rounded-md border border-input bg-input px-1.5 py-0.5 text-center text-foreground"
      />
      quiet minutes{minutes === 0 ? ' (off)' : ''}
      {saved && <span className="text-success">saved</span>}
    </label>
  )
}
