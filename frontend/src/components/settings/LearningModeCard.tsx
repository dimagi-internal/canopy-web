import { useEffect, useState } from 'react'

import { getMyLearningMode, setMyLearningMode, type LearningMode } from '@/api/learningMode'

/** Settings → Learning, first: your learning mode. While it is on, your coaches, the owners and admins of your
 *  workspaces and the admins of the agents you talk to may read your sessions, read-only, never a sensitive one.
 *  Shows who turned it on (you, or an owner of one of your workspaces) and the exact list of who reads. */
export function LearningModeCard() {
  const [data, setData] = useState<LearningMode | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => {
    getMyLearningMode()
      .then(setData)
      .catch((e: unknown) => setError(e instanceof Error ? e.message : 'Could not load'))
  }, [])

  if (!data) return error ? <p className="text-sm text-destructive">{error}</p> : null

  const change = async (on: boolean) => {
    setBusy(true)
    setError('')
    try {
      setData(await setMyLearningMode(on))
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not change')
    } finally {
      setBusy(false)
    }
  }

  const { readers } = data
  const by = data.set_by
  return (
    <section className="rounded-xl border border-border bg-card p-5 space-y-3" aria-label="Learning mode">
      <div className="flex items-center justify-between gap-3">
        <h2 className="text-sm font-semibold text-foreground">Learning mode</h2>
        <label className="flex items-center gap-2 text-sm">
          <input
            type="checkbox"
            checked={data.on}
            disabled={busy}
            aria-label="Learning mode"
            onChange={(e) => void change(e.target.checked)}
          />
          {data.on ? 'On' : 'Off'}
        </label>
      </div>
      <p className="text-xs text-muted-foreground">
        In learning mode, coaching agents and admins may read and analyze your sessions to help you work better:
        read-only, never a sensitive session, and they never ask your machine for more. An owner of one of your
        workspaces may turn it on or off for you there; you are told, and you can always change it here.
      </p>
      {data.on && (
        <p className="text-xs text-foreground" data-testid="learning-mode-set-by">
          Turned on {by ? (by.is_you ? 'by you' : `by ${by.name}`) : ''}
          {data.set_at ? ` on ${new Date(data.set_at).toLocaleDateString()}` : ''}
          {data.workspaces.length > 0 && <> · in {data.workspaces.join(', ')}</>}
        </p>
      )}
      {data.on && (
        <div className="space-y-2 text-xs">
          <h3 className="font-semibold text-foreground">Who can read your sessions</h3>
          <ul className="list-disc space-y-1 pl-5 text-foreground-secondary">
            {readers.coaches.map((c) => (
              <li key={`coach-${c.kind}-${c.slug || c.name}-${c.workspace}`}>
                {c.name} (coach{c.kind === 'agent' ? ', agent' : ''}, {c.workspace})
              </li>
            ))}
            {readers.workspace_admins.map((w) => (
              <li key={`ws-${w.workspace}`}>
                Owners and admins of {w.workspace}: {w.people.map((p) => p.name).join(', ')}
              </li>
            ))}
            {readers.agent_admins.map((a) => (
              <li key={`agent-${a.agent}`}>
                Admins of {a.name} (your sessions with it): {a.people.map((p) => p.name).join(', ')}
              </li>
            ))}
            {readers.coaches.length + readers.workspace_admins.length + readers.agent_admins.length === 0 && (
              <li>Nobody yet.</li>
            )}
          </ul>
        </div>
      )}
      {error && <p className="text-xs text-destructive">{error}</p>}
    </section>
  )
}
