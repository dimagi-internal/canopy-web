import { useCallback, useEffect, useState, type JSX } from 'react'
import { useOutletContext } from 'react-router-dom'
import type { Trigger } from '@/api/triggers'
import { listTriggers, runTriggerNow, updateTrigger } from '@/api/triggers'
import type { AgentOutletContext } from '@/pages/AgentWorkspacePage'
import { describeCron } from '@/components/agents/cronDescribe'
import { relativeAge } from '@/lib/relativeAge'
import { TriggerEditor } from '@/components/agents/TriggerEditor'
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

/** "When" for a trigger: its cadence, its one instant, or the mail that fires it. */
function describeWhen(row: Trigger): string {
  if (row.kind === 'inbound_mail') {
    return `Mail from ${row.sender_address}${row.subject_pattern ? `, subject ~ ${row.subject_pattern}` : ''}`
  }
  if (row.run_once_at) {
    return `Once — ${new Date(row.run_once_at).toLocaleString(undefined, {
      month: 'short',
      day: 'numeric',
      hour: 'numeric',
      minute: '2-digit',
      timeZone: row.timezone,
    })}`
  }
  return describeCron(row.cron, row.timezone)
}

/**
 * "What starts this agent, and when?" in one scan. A dense table of its
 * TRIGGERS — schedules (a cadence or one instant) and inbound mail (alarm mail
 * from a bound sender) — each with its next fire time, its last outcome, who set
 * it up (it fires with their authority, never more), why it is stopped if its
 * creator lost that right, and the things a supervisor does: run a schedule
 * off-cycle, pause, or amend.
 */
export function TriggersSection(): JSX.Element {
  const { agent } = useOutletContext<AgentOutletContext>()
  const [rows, setRows] = useState<Trigger[] | null>(null)
  const [editing, setEditing] = useState<Trigger | 'new' | null>(null)
  const [busy, setBusy] = useState<number | null>(null)
  // Inline error string, the way TaskCard reports a failed board action.
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(() => {
    return listTriggers(agent.slug)
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
      await runTriggerNow(agent.slug, id)
      await load()
    } catch (e) {
      // Without this the button just stops saying "Starting…" and the user is
      // left believing a run they never got was kicked off.
      setError(e instanceof Error ? e.message : 'Could not start that run.')
    } finally {
      setBusy(null)
    }
  }

  async function onToggle(row: Trigger) {
    setError(null)
    try {
      await updateTrigger(agent.slug, row.id, { enabled: !row.enabled })
      await load()
    } catch (e) {
      // The row snaps back on reload; say why, or a failed pause reads as a
      // successful one until the trigger fires anyway.
      setError(e instanceof Error ? e.message : 'Could not change that trigger.')
    }
  }

  return (
    <div className="max-w-4xl px-6 py-8">
      <WorkbenchSubHeader
        title="Triggers"
        count={rows?.length}
        action={
          <button
            type="button"
            onClick={() => setEditing('new')}
            className="rounded bg-primary px-2 py-1 text-[11px] font-medium text-primary-foreground hover:bg-primary/90"
          >
            New trigger
          </button>
        }
      />

      {error && <p className="mb-2 text-[11px] text-destructive">{error}</p>}

      {rows === null ? (
        <WorkbenchSkeleton />
      ) : rows.length === 0 ? (
        <p className="text-[13px] text-muted-foreground">
          Nothing starts {agent.name} on its own yet. Add a trigger to start it on a cadence, or when
          mail from a sender (an alarm, CI) arrives.
        </p>
      ) : (
        <div className="overflow-hidden rounded-lg border border-border bg-card">
          <table className="w-full text-[13px]">
            <thead>
              <tr className="border-b border-border text-left text-[11px] text-muted-foreground">
                <th className="px-3 py-2 font-normal">Trigger</th>
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
                    {row.created_by_email && (
                      <span className="block text-[11px] text-muted-foreground">set up by {row.created_by_email}</span>
                    )}
                    {row.stopped && (
                      <span className="block text-[11px] text-destructive">Stopped: {row.stopped}</span>
                    )}
                  </td>
                  <td className="px-3 py-2 text-foreground-secondary">{describeWhen(row)}</td>
                  <td className="px-3 py-2 text-muted-foreground">
                    {!row.enabled
                      ? row.run_once_at && row.last_slot
                        ? 'done'
                        : 'paused'
                      : row.kind === 'inbound_mail'
                        ? 'on mail'
                        : row.next_runs?.[0]
                          ? relativeAge(row.next_runs[0], new Date(), { future: true, seconds: false })
                          : '—'}
                  </td>
                  <td className="px-3 py-2">
                    <StatusChip status={row.last_status} />
                  </td>
                  <td className="px-3 py-2 text-right whitespace-nowrap">
                    {row.kind === 'cron' && (
                      <button
                        type="button"
                        disabled={busy === row.id}
                        onClick={() => void onRunNow(row.id)}
                        className="rounded border border-input px-2 py-0.5 text-[11px] text-foreground-secondary hover:bg-muted disabled:opacity-50"
                      >
                        {busy === row.id ? 'Starting…' : 'Run now'}
                      </button>
                    )}
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
        <TriggerEditor
          agentSlug={agent.slug}
          agentClis={agent.clis}
          trigger={editing === 'new' ? null : editing}
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
