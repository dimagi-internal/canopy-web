import { useCallback, useEffect, useState, type JSX } from 'react'
import { useOutletContext } from 'react-router-dom'
import type { Schedule } from '@/api/schedules'
import { listSchedules, runScheduleNow, updateSchedule } from '@/api/schedules'
import type { AgentOutletContext } from '@/pages/AgentWorkspacePage'
import { describeCron, relative } from '@/components/agents/cronDescribe'
import { ScheduleEditor } from '@/components/agents/ScheduleEditor'
import { WorkbenchSubHeader, WorkbenchSkeleton } from 'canopy-ui'

function StatusChip({ status }: { status: string }): JSX.Element {
  if (!status) return <span className="text-foreground-subtle">—</span>
  const tone =
    status === 'done'
      ? 'bg-success/10 text-success border-success/30'
      : status === 'missed' || status === 'failed' || status === 'lost' || status === 'cancelled'
        ? 'bg-warning/10 text-warning border-warning/30'
        : 'bg-info/10 text-info border-info/30'
  return <span className={`inline-flex rounded border px-1.5 py-0.5 text-[11px] ${tone}`}>{status}</span>
}

/**
 * "What does this agent do on a cadence, and when does it next do it?" in one
 * scan. A dense table: each row is a declared recurring activity with its
 * server-computed next fire time, its last outcome, and the three things a
 * supervisor actually does — run it off-cycle, pause it, or amend it.
 */
export function SchedulesSection(): JSX.Element {
  const { agent } = useOutletContext<AgentOutletContext>()
  const [rows, setRows] = useState<Schedule[] | null>(null)
  const [editing, setEditing] = useState<Schedule | 'new' | null>(null)
  const [busy, setBusy] = useState<number | null>(null)
  // Inline error string, the way TaskCard reports a failed board action.
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(() => {
    return listSchedules(agent.slug)
      .then((items) => setRows(items))
      .catch(() => setRows([]))
  }, [agent.slug])

  useEffect(() => {
    setRows(null)
    void load()
  }, [agent.slug, load])

  async function onRunNow(id: number) {
    setBusy(id)
    setError(null)
    try {
      await runScheduleNow(agent.slug, id)
      await load()
    } catch (e) {
      // Without this the button just stops saying "Starting…" and the user is
      // left believing a run they never got was kicked off.
      setError(e instanceof Error ? e.message : 'Could not start that run.')
    } finally {
      setBusy(null)
    }
  }

  async function onToggle(row: Schedule) {
    setError(null)
    try {
      await updateSchedule(agent.slug, row.id, { enabled: !row.enabled })
      await load()
    } catch (e) {
      // The row snaps back on reload; say why, or a failed pause reads as a
      // successful one until the schedule fires anyway.
      setError(e instanceof Error ? e.message : 'Could not change that schedule.')
    }
  }

  return (
    <div className="max-w-4xl px-6 py-8">
      <WorkbenchSubHeader
        title="Schedules"
        count={rows?.length}
        action={
          <button
            type="button"
            onClick={() => setEditing('new')}
            className="rounded bg-primary px-2 py-1 text-[11px] font-medium text-primary-foreground hover:bg-primary/90"
          >
            New schedule
          </button>
        }
      />

      {error && <p className="mb-2 text-[11px] text-destructive">{error}</p>}

      {rows === null ? (
        <WorkbenchSkeleton />
      ) : rows.length === 0 ? (
        <p className="text-[13px] text-muted-foreground">
          No recurring activities yet. Add one to have {agent.name} start it on a cadence.
        </p>
      ) : (
        <div className="overflow-hidden rounded-lg border border-border bg-card">
          <table className="w-full text-[13px]">
            <thead>
              <tr className="border-b border-border text-left text-[11px] text-muted-foreground">
                <th className="px-3 py-2 font-normal">Schedule</th>
                <th className="px-3 py-2 font-normal">When</th>
                <th className="px-3 py-2 font-normal">Next</th>
                <th className="px-3 py-2 font-normal">Last</th>
                <th className="px-3 py-2" />
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.id} className="border-b border-border/50 last:border-0">
                  <td className="px-3 py-2">
                    <span className={row.enabled ? 'text-foreground' : 'text-foreground-subtle'}>{row.name}</span>
                  </td>
                  <td className="px-3 py-2 text-foreground-secondary">{describeCron(row.cron, row.timezone)}</td>
                  <td className="px-3 py-2 text-muted-foreground">
                    {row.enabled ? relative(row.next_runs?.[0]) : 'paused'}
                  </td>
                  <td className="px-3 py-2">
                    <StatusChip status={row.last_status} />
                  </td>
                  <td className="px-3 py-2 text-right whitespace-nowrap">
                    <button
                      type="button"
                      disabled={busy === row.id}
                      onClick={() => void onRunNow(row.id)}
                      className="rounded border border-input px-2 py-0.5 text-[11px] text-foreground-secondary hover:bg-muted disabled:opacity-50"
                    >
                      {busy === row.id ? 'Starting…' : 'Run now'}
                    </button>
                    <button
                      type="button"
                      onClick={() => void onToggle(row)}
                      className="ml-2 rounded border border-input px-2 py-0.5 text-[11px] text-foreground-secondary hover:bg-muted"
                    >
                      {row.enabled ? 'Pause' : 'Resume'}
                    </button>
                    <button
                      type="button"
                      onClick={() => setEditing(row)}
                      className="ml-2 rounded border border-input px-2 py-0.5 text-[11px] text-foreground-secondary hover:bg-muted"
                    >
                      Edit
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {editing && (
        <ScheduleEditor
          agentSlug={agent.slug}
          schedule={editing === 'new' ? null : editing}
          onClose={() => setEditing(null)}
          onSaved={() => {
            setEditing(null)
            void load()
          }}
        />
      )}
    </div>
  )
}
