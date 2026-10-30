import { useCallback, useEffect, useState, type JSX } from 'react'
import {
  deleteProjectCheckin,
  listProjectCheckins,
  runProjectCheckinNow,
  type Checkin,
  type ProjectKey,
} from '@/api/checkins'
import { RUN_STATUS } from './checkinModel'
import { slotTime, andList, CheckinForm } from './CheckinParts'

// A PROJECT'S CHECK-INS — its recurring meetings (spec
// 2026-10-17-project-check-ins-design.md §4). The panel lists each check-in's
// name, cadence, lead, channel and agents, with Edit, Delete and Run now for
// whoever may change them (the project's owner, a workspace owner or admin);
// the timeline lists every run, newest first, with who took part, what was
// read, who confirmed it, the huddle, and what it proposed. Run now announces
// in Slack (or, with no channel, starts the agent's turn) — it never reads a
// channel; that only happens when the lead presses Confirm there.

export function CheckinsPanel({
  project,
  projectAgent,
  canAdd,
  onRan,
}: {
  project: ProjectKey
  /** The project's own agent — always one of a check-in's agents. */
  projectAgent: string
  /** Show "Add a check-in" (the server still decides). */
  canAdd: boolean
  /** A run was started here: the timeline should re-read. */
  onRan: () => void
}): JSX.Element {
  const [rows, setRows] = useState<Checkin[] | null>(null)
  const [error, setError] = useState('')
  const [note, setNote] = useState('')
  const [editing, setEditing] = useState<Checkin | 'new' | null>(null)
  const [busy, setBusy] = useState<string | null>(null)
  const key = `${project.agent}/${project.ref}/${project.workspace ?? ''}`

  const load = useCallback(
    () =>
      listProjectCheckins(project)
        .then((r) => {
          setRows(r)
          setError('')
        })
        .catch((e: unknown) => setError(e instanceof Error ? e.message : String(e))),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [key],
  )
  useEffect(() => {
    void load()
  }, [load])

  const act = (c: Checkin, what: 'run' | 'delete') => {
    if (what === 'delete' && !window.confirm(`Delete the check-in “${c.name}”? Its past runs stay on the timeline.`)) return
    setBusy(`${what}:${c.id}`)
    setNote('')
    setError('')
    const work =
      what === 'run'
        ? runProjectCheckinNow(project, c.name).then((r) =>
            setNote(
              r.status === 'awaiting_confirm'
                ? `Announced in #${c.channel_name}. Nothing is read until ${c.lead_email || 'the lead'} confirms there.`
                : r.status === 'running'
                  ? `${c.agent}'s turn is queued.`
                  : `The run ${RUN_STATUS[r.status] ?? r.status}${r.note ? `: ${r.note}` : ''}.`,
            ),
          )
        : deleteProjectCheckin(project, c.name)
    void work
      .then(() => {
        onRan()
        return load()
      })
      .catch((e: unknown) => setError(e instanceof Error ? e.message : String(e)))
      .finally(() => setBusy(null))
  }

  return (
    <div className="space-y-3" data-testid="checkins">
      {error && <p className="text-[12px] text-destructive">{error}</p>}
      {note && <p className="text-[12px] text-muted-foreground" role="status">{note}</p>}
      {rows !== null && rows.length === 0 && editing === null && (
        <p className="text-[13px] text-muted-foreground">
          No check-ins yet. A check-in is a recurring meeting on this project: its agent (or a huddle it leads) works
          the project on a cadence, and what it proposes lands here as tasks to start or decline.
        </p>
      )}
      {rows !== null && rows.length > 0 && (
        <ul className="flex flex-col gap-2">
          {rows.map((c) => (
            <li key={c.id} data-checkin={c.name} className="flex flex-wrap items-start justify-between gap-3 rounded-xl border border-border bg-card p-3">
              <div className="min-w-0">
                <p className="font-medium text-foreground">
                  {c.name}
                  {!c.enabled && <span className="ml-2 text-[11px] font-normal text-muted-foreground">(paused)</span>}
                  <span className="ml-2 text-[11px] font-normal text-muted-foreground">
                    {c.format === 'huddle' ? `a huddle led by ${c.agent}` : `${c.agent} alone`}
                  </span>
                </p>
                <p className="text-[12px] text-muted-foreground">
                  <code>{c.cron}</code> {c.timezone}
                  {c.next_runs?.[0] ? ` · next ${slotTime(c.next_runs[0])}` : ''} · lead {c.lead_email || '—'} ·{' '}
                  {c.channel_id ? `#${c.channel_name || c.channel_id}` : 'no channel'} · {andList(c.agents ?? [])}
                  {c.last_run_status ? ` · last run ${RUN_STATUS[c.last_run_status] ?? c.last_run_status}` : ''}
                </p>
              </div>
              {c.can_edit && editing === null && (
                <div className="flex gap-3 text-[12px]">
                  <button type="button" disabled={busy !== null} onClick={() => act(c, 'run')}
                    className="text-muted-foreground hover:text-primary disabled:opacity-50">
                    {busy === `run:${c.id}` ? 'Running…' : 'Run now'}
                  </button>
                  <button type="button" onClick={() => setEditing(c)} className="text-muted-foreground hover:text-primary">
                    Edit
                  </button>
                  <button type="button" disabled={busy !== null} onClick={() => act(c, 'delete')}
                    className="text-muted-foreground hover:text-destructive disabled:opacity-50">
                    Delete
                  </button>
                </div>
              )}
            </li>
          ))}
        </ul>
      )}
      {editing !== null ? (
        <CheckinForm
          project={project}
          projectAgent={projectAgent}
          checkin={editing === 'new' ? null : editing}
          onDone={(saved) => {
            setEditing(null)
            if (saved) void load()
          }}
        />
      ) : (
        canAdd && (
          <button
            type="button"
            onClick={() => setEditing('new')}
            className="rounded-md border border-border px-3 py-1 text-[13px] hover:border-primary/50 hover:text-primary"
          >
            Add a check-in
          </button>
        )
      )}
    </div>
  )
}

