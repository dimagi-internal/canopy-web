import { PRESETS } from './triggerTime'

// The two halves of the trigger form that differ by kind: a schedule's When and its next-runs preview, and a mail trigger's sender and subject. The form, its state and its save are `TriggerEditor`.

export function MailFields({ sender, setSender, subjectPattern, setSubjectPattern }: {
  sender: string
  setSender: (v: string) => void
  subjectPattern: string
  setSubjectPattern: (v: string) => void
}) {
  return (
    <>
        <label className="mb-1 block text-[11px] text-muted-foreground">From (sender address)</label>
        <input
          value={sender}
          onChange={(e) => setSender(e.target.value)}
          placeholder="no-reply@sns.amazonaws.com"
          className="mb-3 w-full rounded border border-input bg-input px-2 py-1 font-mono text-[13px] text-foreground"
        />
        <label className="mb-1 block text-[11px] text-muted-foreground">
          Subject matches (a regular expression; blank = any)
        </label>
        <input
          value={subjectPattern}
          onChange={(e) => setSubjectPattern(e.target.value)}
          placeholder='^(ALARM|OK): "labs-'
          className="mb-2 w-full rounded border border-input bg-input px-2 py-1 font-mono text-[13px] text-foreground"
        />
        <p className="mb-3 text-[11px] text-muted-foreground">
          Fires only for mail that proves it is from this address (DMARC, or DKIM signed by its
          domain). Only an admin of the agent can set this up, and it acts with your authority.
        </p>
    </>
  )
}

export function ScheduleFields({
  once, setOnce, cron, setCron, tz, setTz, runOnceAt, setRunOnceAt, preview, previewError,
}: {
  once: boolean
  setOnce: (v: boolean) => void
  cron: string
  setCron: (v: string) => void
  tz: string
  setTz: (v: string) => void
  runOnceAt: string
  setRunOnceAt: (v: string) => void
  preview: string[]
  previewError: string
}) {
  return (
    <>
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
    </>
  )
}
