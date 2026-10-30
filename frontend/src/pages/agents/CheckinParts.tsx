// A project check-in's form and its timeline, and the helpers they share. The panel that lists them is `CheckinsPanel`.
import { useEffect, useState, type JSX } from 'react'
import { Link } from 'react-router-dom'
import { listAgents } from '@/api/agents'
import {
  createProjectCheckin,
  listProjectCheckinRuns,
  updateProjectCheckin,
  type Checkin,
  type CheckinTimelineRun,
  type ProjectKey,
} from '@/api/checkins'
import { listMembers } from '@/api/workspaces'
import { readWindow, RUN_STATUS } from './checkinModel'

export function slotTime(iso: string | null | undefined): string {
  return iso
    ? new Date(iso).toLocaleString(undefined, { weekday: 'short', month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' })
    : ''
}

export function andList(xs: readonly string[]): string {
  return xs.length <= 1 ? (xs[0] ?? '') : `${xs.slice(0, -1).join(', ')} and ${xs[xs.length - 1]}`
}

export function Field({ label, children, hint }: { label: string; children: React.ReactNode; hint?: string }) {
  return (
    <label className="flex flex-col gap-1 text-[13px]">
      <span className="font-medium text-foreground">{label}</span>
      {children}
      {hint && <span className="text-[11px] text-muted-foreground">{hint}</span>}
    </label>
  )
}

export const input = 'rounded-md border border-border bg-background px-2 py-1 text-[13px]'

export function CheckinForm({
  project,
  projectAgent,
  checkin,
  onDone,
}: {
  project: ProjectKey
  projectAgent: string
  checkin: Checkin | null
  onDone: (saved: boolean) => void
}) {
  const [name, setName] = useState(checkin?.name ?? 'Weekly check-in')
  const [topic, setTopic] = useState(checkin?.topic ?? '')
  const [channel, setChannel] = useState(checkin?.channel_id ? `#${checkin.channel_name || checkin.channel_id}` : '')
  const [lead, setLead] = useState(checkin?.lead_email ?? '')
  const [agents, setAgents] = useState<string[]>(checkin ? [...(checkin.agents ?? [])] : [projectAgent])
  const [cron, setCron] = useState(checkin?.cron ?? '0 9 * * 1')
  const [tz, setTz] = useState(checkin?.timezone ?? Intl.DateTimeFormat().resolvedOptions().timeZone ?? 'UTC')
  const [enabled, setEnabled] = useState(checkin?.enabled ?? true)
  const [allAgents, setAllAgents] = useState<string[]>([])
  const [emails, setEmails] = useState<string[]>([])
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    listAgents({ limit: 200 }).then((p) => setAllAgents(p.items.map((a) => a.slug))).catch(() => undefined)
    if (project.workspace) listMembers(project.workspace).then((m) => setEmails(m.map((x) => x.email))).catch(() => undefined)
  }, [project.workspace])

  const toggle = (s: string) =>
    s === projectAgent ? undefined : setAgents((a) => (a.includes(s) ? a.filter((x) => x !== s) : [...a, s]))

  const save = async (e: React.FormEvent) => {
    e.preventDefault()
    setBusy(true)
    setError('')
    const body = { name, topic, channel: channel.trim(), lead_email: lead, agents, cron, timezone: tz, enabled }
    try {
      if (checkin) await updateProjectCheckin(project, checkin.name, body)
      else await createProjectCheckin(project, body)
      onDone(true)
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <form onSubmit={save} aria-label={checkin ? 'Edit check-in' : 'New check-in'}
      className="grid gap-3 rounded-xl border border-border bg-card p-4 sm:grid-cols-2">
      <Field label="Name"><input className={input} value={name} onChange={(e) => setName(e.target.value)} required /></Field>
      <Field label="Lead" hint="The person each run answers to — a member of this workspace (and, with a channel, of it).">
        <input className={input} type="email" list="checkin-members" value={lead}
          onChange={(e) => setLead(e.target.value)} required />
        <datalist id="checkin-members">{emails.map((m) => <option key={m} value={m} />)}</datalist>
      </Field>
      <div className="sm:col-span-2">
        <Field label="Topic" hint="The brief the meeting works from. Blank: the project's outcome.">
          <textarea className={input} rows={3} value={topic} onChange={(e) => setTopic(e.target.value)} />
        </Field>
      </div>
      <Field label="Slack channel (optional)"
        hint="#channel-name or its id. With one, each run is announced there and read only when the lead confirms. Without one, nothing is read.">
        <input className={input} value={channel} onChange={(e) => setChannel(e.target.value)} />
      </Field>
      <div>
        <Field label="Agents" hint={`${projectAgent} always takes part; more agents make it a huddle ${projectAgent} leads.`}>
          <div className="flex flex-wrap gap-2">
            {(allAgents.length ? allAgents : [projectAgent]).map((s) => (
              <label key={s} className="inline-flex items-center gap-1 text-[12px]">
                <input type="checkbox" checked={agents.includes(s)} disabled={s === projectAgent} onChange={() => toggle(s)} /> {s}
              </label>
            ))}
          </div>
        </Field>
      </div>
      <Field label="Cadence (cron)" hint="e.g. 0 9 * * 2 — Tuesdays at 9">
        <input className={input} value={cron} onChange={(e) => setCron(e.target.value)} required />
      </Field>
      <Field label="Timezone"><input className={input} value={tz} onChange={(e) => setTz(e.target.value)} required /></Field>
      <label className="inline-flex items-center gap-2 text-[13px]">
        <input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} /> Enabled
      </label>
      {error && <p role="alert" className="text-[13px] text-destructive sm:col-span-2">{error}</p>}
      <div className="flex gap-2 sm:col-span-2">
        <button type="submit" disabled={busy}
          className="rounded-md bg-primary px-3 py-1 text-[13px] text-primary-foreground disabled:opacity-50">
          {busy ? 'Saving…' : checkin ? 'Save' : 'Add'}
        </button>
        <button type="button" onClick={() => onDone(false)} className="rounded-md px-3 py-1 text-[13px] text-muted-foreground">
          Cancel
        </button>
      </div>
    </form>
  )
}

/** Every check-in run on the project, newest first. `version` re-reads it. */
export function CheckinTimeline({ project, version = 0 }: { project: ProjectKey; version?: number }): JSX.Element | null {
  const [runs, setRuns] = useState<CheckinTimelineRun[] | null>(null)
  const [error, setError] = useState('')
  const key = `${project.agent}/${project.ref}/${project.workspace ?? ''}`
  useEffect(() => {
    let alive = true
    listProjectCheckinRuns(project)
      .then((r) => alive && setRuns(r))
      .catch((e: unknown) => alive && setError(e instanceof Error ? e.message : String(e)))
    return () => {
      alive = false
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, version])
  if (error) return <p className="text-[12px] text-destructive">Couldn’t load the timeline: {error}</p>
  if (runs === null) return null
  if (runs.length === 0) return <p className="text-[13px] text-muted-foreground">No check-in has run yet.</p>
  return (
    <ol className="space-y-2" data-testid="checkin-timeline">
      {runs.map((r) => {
        const parts = r.participants
        const tasks = r.tasks ?? []
        const spawned = [...tasks.flatMap((t) => t.spawned ?? []), ...(r.projects ?? [])]
        return (
          <li key={r.id} data-run={r.id} className="rounded-xl border border-border bg-card p-3 text-[13px]">
            <div className="flex flex-wrap items-baseline justify-between gap-2">
              <p className="font-medium text-foreground">
                {slotTime(r.slot)} · {r.checkin_name || 'Check-in'}
                {r.manual && <span className="ml-1 text-[11px] font-normal text-muted-foreground">(run now)</span>}
              </p>
              <span className="text-[12px] text-muted-foreground">{RUN_STATUS[r.status] ?? r.status}</span>
            </div>
            <p className="mt-1 text-[12px] text-muted-foreground">
              {parts?.agents?.length ? andList(parts.agents) : '—'}
              {parts?.lead_email ? ` · lead ${parts.lead_email}` : ''} · {readWindow(r)}
              {r.confirmed_by_email ? ` · confirmed by ${r.confirmed_by_email}` : ''}
              {r.note ? ` · ${r.note}` : ''}
            </p>
            <p className="mt-1 flex flex-wrap gap-x-3 text-[12px]">
              {r.huddle_url && <Link to={r.huddle_url} className="text-primary hover:underline">The huddle</Link>}
              {r.slack_url && <a href={r.slack_url} target="_blank" rel="noreferrer" className="text-primary hover:underline">Slack post</a>}
            </p>
            {tasks.length > 0 && (
              <ul aria-label="Proposed" className="mt-2 space-y-0.5 text-[12px]">
                {tasks.map((t) => (
                  <li key={`${t.agent}-${t.ref}`}>
                    <span className="text-muted-foreground">{t.ref}</span>{' '}
                    <span className="text-foreground">{t.title}</span>{' '}
                    <span className="text-muted-foreground">({t.status.replace('_', ' ')})</span>
                  </li>
                ))}
              </ul>
            )}
            {spawned.length > 0 && (
              <p className="mt-1 text-[12px] text-muted-foreground">
                Projects:{' '}
                {spawned.map((p, i) => (
                  <span key={`${p.agent}-${p.ref}`}>
                    {i > 0 && ', '}
                    <Link to={p.url} className="text-foreground hover:text-primary hover:underline">{p.agent} {p.ref} {p.name}</Link>{' '}
                    ({p.status})
                  </span>
                ))}
              </p>
            )}
          </li>
        )
      })}
    </ol>
  )
}
