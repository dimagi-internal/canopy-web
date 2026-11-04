import { useEffect, useState, type JSX } from 'react'
import { Button } from 'canopy-ui/ui'
import { getAgent } from '@/api/agents'
import { runTriggerNow } from '@/api/triggers'
import { Markdown } from '@/components/Markdown'
import type { TaskActionName } from '@/api/work'

/**
 * The decision on the review a FAILED PLAIN JOB raises. There is no Start now: approving a
 * task starts a model turn on the agent, and a job runs no model — the server refuses it. What
 * you can do is run the job again (Run now on its schedule, reachable from here), or set the
 * review aside; a later good run closes it by itself.
 */
export function JobReviewDecision({
  agentSlug,
  scheduleId,
  askBody,
  onAct,
}: {
  agentSlug: string
  scheduleId: number
  askBody: string
  onAct: (a: TaskActionName, c?: string) => Promise<void>
}): JSX.Element {
  const [busy, setBusy] = useState(false)
  const [started, setStarted] = useState(false)
  const [error, setError] = useState<string | null>(null)
  // Running a job needs agent admin, as setting one does: offered only to one.
  const [isAdmin, setIsAdmin] = useState<boolean | null>(null)
  useEffect(() => {
    let alive = true
    getAgent(agentSlug)
      .then((a) => alive && setIsAdmin(Boolean(a.is_admin)))
      .catch(() => alive && setIsAdmin(false))
    return () => {
      alive = false
    }
  }, [agentSlug])
  const runAgain = async () => {
    setBusy(true)
    setError(null)
    try {
      await runTriggerNow(agentSlug, scheduleId)
      setStarted(true)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not start the job.')
    } finally {
      setBusy(false)
    }
  }
  const setAside = async (a: TaskActionName) => {
    setBusy(true)
    try {
      await onAct(a)
    } catch {
      /* the error is shown above */
    } finally {
      setBusy(false)
    }
  }
  return (
    <section className="mb-5 rounded-lg border border-warning/30 bg-warning/5 p-3" data-testid="task-decision">
      <p className="mb-1 text-[12px] font-semibold text-warning">A job needs your attention</p>
      {askBody && <Markdown className="mb-2 text-[13px]">{askBody}</Markdown>}
      <div className="flex flex-wrap gap-2">
        {isAdmin && (
          <Button size="sm" disabled={busy || started} onClick={() => void runAgain()}>
            Run now
          </Button>
        )}
        <Button size="sm" variant="outline" disabled={busy} onClick={() => void setAside('snooze')}
                title="Out of Triage for a week, or until something happens on it">
          Snooze
        </Button>
        <Button size="sm" variant="ghost" disabled={busy} onClick={() => void setAside('decline')}>
          Decline
        </Button>
      </div>
      {isAdmin === false && (
        <p className="mt-1.5 text-[11.5px] text-muted-foreground">Only an admin of {agentSlug} can change a job.</p>
      )}
      {started && (
        <p className="mt-1.5 text-[11.5px] text-muted-foreground">
          The job is queued to run again. A good run closes this on its own.
        </p>
      )}
      {error && <p className="mt-1.5 text-[11.5px] text-destructive">{error}</p>}
    </section>
  )
}
