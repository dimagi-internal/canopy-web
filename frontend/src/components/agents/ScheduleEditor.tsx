import { useEffect, useId, useRef, useState } from 'react'
import type { Schedule } from '@/api/schedules'
import { createSchedule, deleteSchedule, previewCron, updateSchedule } from '@/api/schedules'

const PRESETS: { label: string; cron: string }[] = [
  { label: 'Weekly — Friday 9am', cron: '0 9 * * 5' },
  { label: 'Weekly — Monday 9am', cron: '0 9 * * 1' },
  { label: 'Monthly — 1st, 9am', cron: '0 9 1 * *' },
  { label: 'Daily — 9am', cron: '0 9 * * *' },
]

/** `iso` as wall-clock "YYYY-MM-DDTHH:MM" in `tz` — the value a
 * datetime-local input holds. The server reads an offset-less run_once_at in
 * the schedule's timezone, so the client never does zone arithmetic. */
function wallClock(iso: string, tz: string): string {
  const parts = Object.fromEntries(
    new Intl.DateTimeFormat('en-CA', {
      timeZone: tz,
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      hourCycle: 'h23',
    })
      .formatToParts(new Date(iso))
      .map((p) => [p.type, p.value]),
  )
  return `${parts.year}-${parts.month}-${parts.day}T${parts.hour}:${parts.minute}`
}

function tomorrowNineAm(): string {
  const d = new Date()
  d.setDate(d.getDate() + 1)
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T09:00`
}

/**
 * Declare (or amend) one scheduled activity — recurring (cron) or once. The point of the whole surface is
 * the "Next runs" panel: it answers "does this cron mean what I think it
 * means?" at edit time, from the SERVER's own next_slots() — instead of next
 * Friday, when it silently doesn't fire.
 */
export function ScheduleEditor({
  agentSlug,
  schedule,
  onClose,
  onSaved,
}: {
  agentSlug: string
  schedule: Schedule | null
  onClose: () => void
  onSaved: () => void
}) {
  const [name, setName] = useState(schedule?.name ?? '')
  const [prompt, setPrompt] = useState(schedule?.prompt ?? '')
  const [cron, setCron] = useState(schedule?.cron ?? '0 9 * * 5')
  const [tz, setTz] = useState(schedule?.timezone ?? Intl.DateTimeFormat().resolvedOptions().timeZone)
  const [once, setOnce] = useState(Boolean(schedule?.run_once_at))
  const [runOnceAt, setRunOnceAt] = useState(
    schedule?.run_once_at ? wallClock(schedule.run_once_at, schedule.timezone) : tomorrowNineAm(),
  )
  const [error, setError] = useState('')
  const [saving, setSaving] = useState(false)
  const [preview, setPreview] = useState<string[]>([])
  const [previewError, setPreviewError] = useState('')
  const titleId = useId()
  const cardRef = useRef<HTMLDivElement>(null)

  // Esc closes — a modal you can't dismiss from the keyboard is a trap.
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  // Ask the SERVER when this cron would actually run — debounced, because the
  // user is mid-type. Never re-implement cron here: a client parser that says
  // "Fridays" while the server fires Thursdays is the exact failure the preview
  // exists to catch.
  useEffect(() => {
    // A one-off has exactly one run — the time typed — so there is no cron to ask about.
    if (once) return
    let cancelled = false
    const timer = setTimeout(() => {
      previewCron(agentSlug, cron, tz)
        .then((runs) => {
          if (cancelled) return
          setPreview(runs)
          setPreviewError('')
        })
        .catch(() => {
          if (cancelled) return
          setPreview([])
          setPreviewError('Not a valid schedule.')
        })
    }, 300)
    return () => {
      cancelled = true
      clearTimeout(timer)
    }
  }, [agentSlug, cron, tz, once])

  async function onSave() {
    setSaving(true)
    setError('')
    // A one-off sends run_once_at (wall clock, read in `tz` server-side) and no
    // cron; the server derives the cron and disables it after it fires.
    const when = once ? { run_once_at: runOnceAt } : { cron }
    try {
      if (schedule) {
        await updateSchedule(agentSlug, schedule.id, { name, prompt, ...when, timezone: tz })
      } else {
        // enabled / routing / grace_minutes / notify are omitted deliberately:
        // the server's schema owns those defaults, and restating them here would
        // fork them. Not exposed as knobs yet — a schedule you can't reason
        // about is worse than one with fewer knobs.
        await createSchedule(agentSlug, { name, prompt, ...when, timezone: tz })
      }
      onSaved()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not save the schedule.')
    } finally {
      setSaving(false)
    }
  }

  async function onDelete() {
    if (!schedule) return
    // Destructive + no undo, and it sits one row away from Cancel/Save.
    // window.confirm is the repo's idiom for this (NarrativeLanding, RunPackage,
    // InsightsPage all gate deletes the same way).
    if (
      !window.confirm(
        `Delete the schedule "${schedule.name}"?\n\nIt will stop running on its cadence. This cannot be undone.`,
      )
    )
      return
    setSaving(true)
    try {
      await deleteSchedule(agentSlug, schedule.id)
      onSaved()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not delete the schedule.')
      setSaving(false)
    }
  }

  return (
    // Scrim tokenized as bg-background/… (the AppLayout mobile-nav precedent) so
    // it dims correctly in BOTH themes — a fixed black scrim only reads right in
    // the light one. Click the scrim (never the card) to close.
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-background/70 p-4"
      onMouseDown={(e) => {
        if (!cardRef.current?.contains(e.target as Node)) onClose()
      }}
    >
      {/* shadow-xl carries the separation: in light theme --background (0.985)
          and --card (1.0) are 0.015 apart, so the border alone leaves the card
          floating on a scrim it barely contrasts with. */}
      <div
        ref={cardRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        className="w-full max-w-lg rounded-lg border border-border bg-card p-4 shadow-xl"
      >
        <h3 id={titleId} className="mb-3 text-sm font-medium text-foreground">
          {schedule ? 'Edit schedule' : 'New schedule'}
        </h3>

        <label className="mb-1 block text-[11px] text-muted-foreground">Name</label>
        <input
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="Weekly manager report"
          className="mb-3 w-full rounded border border-input bg-input px-2 py-1 text-[13px] text-foreground"
        />

        <label className="mb-1 block text-[11px] text-muted-foreground">Prompt</label>
        <input
          value={prompt}
          onChange={(e) => setPrompt(e.target.value)}
          placeholder="/echo:manager-report"
          className="mb-3 w-full rounded border border-input bg-input px-2 py-1 font-mono text-[13px] text-foreground"
        />

        <label className="mb-1 block text-[11px] text-muted-foreground">When</label>
        <div className="mb-2 flex gap-1" role="group" aria-label="Repeat">
          {[
            { label: 'Repeats', value: false },
            { label: 'Once', value: true },
          ].map((m) => (
            <button
              key={m.label}
              type="button"
              aria-pressed={once === m.value}
              onClick={() => setOnce(m.value)}
              className={`rounded border px-2 py-0.5 text-[11px] ${
                once === m.value
                  ? 'border-primary text-primary'
                  : 'border-input text-foreground-secondary hover:bg-muted'
              }`}
            >
              {m.label}
            </button>
          ))}
        </div>
        {!once && (
          <div className="mb-2 flex flex-wrap gap-1">
            {PRESETS.map((p) => (
              <button
                key={p.cron}
                type="button"
                onClick={() => setCron(p.cron)}
                className={`rounded border px-2 py-0.5 text-[11px] ${
                  cron === p.cron
                    ? 'border-primary text-primary'
                    : 'border-input text-foreground-secondary hover:bg-muted'
                }`}
              >
                {p.label}
              </button>
            ))}
          </div>
        )}
        <div className="mb-3 flex gap-2">
          {once ? (
            <input
              type="datetime-local"
              value={runOnceAt}
              onChange={(e) => setRunOnceAt(e.target.value)}
              aria-label="Run once at"
              className="w-52 rounded border border-input bg-input px-2 py-1 text-[13px] text-foreground"
            />
          ) : (
            <input
              value={cron}
              onChange={(e) => setCron(e.target.value)}
              aria-label="Cron expression"
              className="w-40 rounded border border-input bg-input px-2 py-1 font-mono text-[13px] text-foreground"
            />
          )}
          <input
            value={tz}
            onChange={(e) => setTz(e.target.value)}
            aria-label="Timezone"
            className="flex-1 rounded border border-input bg-input px-2 py-1 text-[13px] text-foreground"
          />
        </div>

        <div className="mb-3 rounded border border-border bg-muted/40 px-2 py-1.5">
          <p className="mb-0.5 text-[11px] text-muted-foreground">{once ? 'Runs once' : 'Next runs'}</p>
          {once ? (
            <p className="text-[11px] text-foreground-secondary">
              {runOnceAt ? `${runOnceAt.replace('T', ' ')} ${tz} — then it turns itself off` : '—'}
            </p>
          ) : previewError ? (
            <p className="text-[11px] text-destructive">{previewError}</p>
          ) : preview.length === 0 ? (
            <p className="text-[11px] text-foreground-subtle">—</p>
          ) : (
            <ul className="text-[11px] text-foreground-secondary">
              {preview.map((iso) => (
                <li key={iso}>
                  {new Date(iso).toLocaleString(undefined, {
                    weekday: 'short',
                    month: 'short',
                    day: 'numeric',
                    hour: '2-digit',
                    minute: '2-digit',
                  })}
                </li>
              ))}
            </ul>
          )}
        </div>

        {error && <p className="mb-3 text-[11px] text-destructive">{error}</p>}

        <div className="flex items-center justify-between">
          <div>
            {schedule && (
              <button
                type="button"
                onClick={() => void onDelete()}
                className="text-[11px] text-destructive hover:underline"
              >
                Delete
              </button>
            )}
          </div>
          <div className="flex gap-2">
            <button
              type="button"
              onClick={onClose}
              className="rounded border border-input px-3 py-1 text-[11px] text-foreground-secondary hover:bg-muted"
            >
              Cancel
            </button>
            <button
              type="button"
              disabled={saving || !name.trim() || !prompt.trim() || (once && !runOnceAt)}
              onClick={() => void onSave()}
              className="rounded bg-primary px-3 py-1 text-[11px] text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
            >
              {saving ? 'Saving…' : 'Save'}
            </button>
          </div>
        </div>
      </div>
    </div>
  )
}
