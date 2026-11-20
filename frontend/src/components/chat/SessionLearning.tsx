import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'

import {
  declareMySessionProject,
  getMySessionLearning,
  removeMySession,
  restoreMySession,
  undeclareMySessionProject,
  type SessionLearning,
} from '@/api/learning'
import { errorText } from '@/api/problem'
import { listProjects, type Project } from '@/api/work'

/** This session's place in canopy learning, for a person who is in it — `null` for anyone else.
 *  The route answers 404 to someone not in the session and 403 to anything that is not a person
 *  (an agent's login, a turn, a runner, a site's widget), so the item simply never shows for them. */
export function useSessionLearning(sessionId: string | undefined) {
  const [state, setState] = useState<SessionLearning | null>(null)
  useEffect(() => {
    let live = true
    setState(null)
    if (!sessionId) return
    getMySessionLearning(sessionId)
      .then((s) => live && setState(s))
      .catch(() => live && setState(null))
    return () => {
      live = false
    }
  }, [sessionId])
  return [state, setState] as const
}

const TIER_WORDS: Record<string, string> = {
  core: 'belongs to it',
  adjacent: 'touched it',
  candidate: 'may belong to it',
  rejected: 'does not belong to it',
}

const HOW_WORDS: Record<string, string> = {
  dispatched: 'it was started for the project',
  signals: 'canopy guessed from what it touched',
  judged: 'canopy’s judge decided',
  declared: 'someone in it said so',
}

const BUTTON = 'rounded-md border border-border px-2 py-0.5 hover:bg-muted disabled:opacity-50'

/** Remove from learning / let learning use it again, and say which projects it is tied to. Every act here
 *  is the person's own (the routes are person-only and off MCP). Each sentence is a claim about
 *  `apps/brain/api.py`, `consent.exclude|include` and `cascade.py`. */
export function SessionLearningPanel({
  sessionId,
  workspace,
  state,
  onChange,
}: {
  sessionId: string
  workspace?: string
  state: SessionLearning
  onChange: (next: SessionLearning) => void
}) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [projects, setProjects] = useState<Project[] | null>(null)
  const [adding, setAdding] = useState('')

  useEffect(() => {
    if (!workspace) return
    let live = true
    listProjects('open', workspace)
      .then((ps) => live && setProjects(ps.filter((p) => p.workspace === workspace)))
      .catch(() => live && setProjects([]))
    return () => {
      live = false
    }
  }, [workspace])

  const act = async (run: () => Promise<SessionLearning>) => {
    setBusy(true)
    setError('')
    try {
      onChange(await run())
    } catch (e) {
      setError(errorText(e, 'Not saved — try again.'))
    } finally {
      setBusy(false)
    }
  }

  const links = state.projects ?? []
  const linked = new Set(links.map((p) => p.project))
  const unlinked = (projects ?? []).filter((p) => !linked.has(p.handle))

  return (
    <div className="space-y-3 text-[12px]">
      <section className="space-y-1.5">
        <p className="font-medium text-foreground" data-testid="session-learning-status">
          {state.excluded ? 'Removed from learning' : 'Learning may use this session'}
        </p>
        <p className="text-muted-foreground">
          {state.excluded
            ? 'You took it out: nothing may learn from it, including coaching, and what was learned from it was withdrawn.'
            : 'Within what you agreed to in Settings → Learning, and what everyone else in it agreed to.'}
        </p>
        {state.excluded ? (
          <>
            <button type="button" className={BUTTON} disabled={busy}
                    onClick={() => void act(() => restoreMySession(sessionId))}>
              Let learning use this again
            </button>
            <p className="text-muted-foreground">
              It can be used from now on; what was already withdrawn stays withdrawn.
            </p>
          </>
        ) : (
          <>
            <button type="button" className={`${BUTTON} text-destructive`} disabled={busy}
                    onClick={() => void act(() => removeMySession(sessionId))}>
              Remove from learning
            </button>
            <p className="text-muted-foreground">
              Nothing may learn from this session again, coaching included, and what was learned from it is
              withdrawn everywhere. If others were in it, it also leaves every shared use (projects, canopy);
              what was learned about them from their own words stays.
            </p>
          </>
        )}
      </section>

      <section className="space-y-1.5 border-t border-border pt-2">
        <p className="font-medium text-foreground">Projects</p>
        <p className="text-muted-foreground">
          A project’s readers may read a session tied to it only if everyone in the session agreed to project
          learning. Your call overrides canopy’s guess.
        </p>
        {links.length === 0 ? (
          <p className="text-muted-foreground">Not tied to any project you can see.</p>
        ) : (
          <ul className="space-y-1.5">
            {links.map((p) => (
              <li key={p.project} className="flex flex-wrap items-center justify-between gap-2"
                  data-testid={`session-learning-project-${p.project}`}>
                <span className="min-w-0">
                  <span className="text-foreground">{p.name || p.project}</span>{' '}
                  <span className="text-muted-foreground">
                    — {TIER_WORDS[p.tier] ?? p.tier} ({HOW_WORDS[p.how] ?? p.how})
                  </span>
                </span>
                <span className="flex gap-1.5">
                  {p.tier !== 'rejected' && (
                    <button type="button" className={BUTTON} disabled={busy}
                            onClick={() => void act(() => declareMySessionProject(sessionId, p.project, 'rejected'))}>
                      Not this project
                    </button>
                  )}
                  {p.tier !== 'core' && (
                    <button type="button" className={BUTTON} disabled={busy}
                            onClick={() => void act(() => declareMySessionProject(sessionId, p.project, 'core'))}>
                      Belongs here
                    </button>
                  )}
                  {p.how === 'declared' && (
                    <button type="button" className={BUTTON} disabled={busy}
                            onClick={() => void act(() => undeclareMySessionProject(sessionId, p.project))}>
                      Take back my call
                    </button>
                  )}
                </span>
              </li>
            ))}
          </ul>
        )}
        {unlinked.length > 0 && (
          <div className="flex items-center gap-1.5">
            <select
              aria-label="Tie this session to a project"
              value={adding}
              onChange={(e) => setAdding(e.target.value)}
              className="min-w-0 flex-1 rounded-md border border-input bg-background px-1.5 py-0.5"
            >
              <option value="">Tie it to a project…</option>
              {unlinked.map((p) => (
                <option key={p.handle} value={p.handle}>{p.name}</option>
              ))}
            </select>
            <button type="button" className={BUTTON} disabled={busy || !adding}
                    onClick={() => void act(async () => {
                      const next = await declareMySessionProject(sessionId, adding, 'core')
                      setAdding('')
                      return next
                    })}>
              Add
            </button>
          </div>
        )}
      </section>

      {error && <p className="text-destructive">{error}</p>}
      <p className="border-t border-border pt-1.5 text-muted-foreground">
        <Link to="/settings/learning" className="underline hover:text-foreground">
          What you agreed to, and the sessions you took out
        </Link>
      </p>
    </div>
  )
}
