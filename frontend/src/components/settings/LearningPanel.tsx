import { useCallback, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'

import {
  endMyLearning,
  getMyDisclosure,
  getMyLearning,
  listMyRemovedSessions,
  listReporting,
  restoreMySession,
  setMyLearning,
  type DisclosureDay,
  type Learning,
  type RemovedSession,
  type ReportingLine,
} from '@/api/learning'
import { listWorkspaces } from '@/api/workspaces'
import { useAuth } from '@/auth/AuthProvider'
import { scopedPath } from '@/lib/scopedLinks'
import { Td, Th, Tr } from '@/components/DataTable'

/** Settings → Learning: what canopy may learn from your sessions, by class; who sees what it produces; the
 *  sessions you took out (and letting one back in); who manages you; and what was read of your words. Every change is yours alone
 *  (the routes refuse anyone else) and takes effect at once. */
export function LearningPanel() {
  const [data, setData] = useState<Learning | null>(null)
  const [removed, setRemoved] = useState<RemovedSession[]>([])
  const [days, setDays] = useState<DisclosureDay[]>([])
  const [lines, setLines] = useState<ReportingLine[]>([])
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const me = useAuth().user?.user_id ?? null

  const load = useCallback(async () => {
    try {
      setData(await getMyLearning())
      setRemoved(await listMyRemovedSessions())
      setDays(await getMyDisclosure())
      const slugs = (await listWorkspaces()).map((w) => w.slug)
      const all = await Promise.all(slugs.map((s) => listReporting(s).catch(() => [] as ReportingLine[])))
      setLines(all.flat())
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not load')
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  if (!data) return error ? <p className="text-sm text-destructive">{error}</p> : null

  const on = new Set(data.classes.filter((c) => c.on).map((c) => c.name))

  const change = async (next: Set<string>) => {
    setBusy(true)
    setError('')
    try {
      // Only the classes: the workspaces and standing rules stay as they are (the server keeps what is
      // not sent), so a toggle never moves the scope or what counts as a workspace joined later.
      const res = next.size ? await setMyLearning({ classes: [...next] }) : await endMyLearning()
      setData(res)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not change')
    } finally {
      setBusy(false)
    }
  }

  const toggle = (name: string, checked: boolean) => {
    const next = new Set(on)
    if (checked) next.add(name)
    else next.delete(name)
    void change(next)
  }

  // The lines a person is managed in. An owner (or a manager) is shown more lines than their own by the
  // route; only the person's own decide the banner and "Who manages you".
  const mine = lines.filter((l) => me !== null && l.person_id === me)
  const managerSeesYou = mine.length > 0

  const restore = async (sessionId: string) => {
    setBusy(true)
    setError('')
    try {
      await restoreMySession(sessionId)
      setRemoved((rs) => rs.filter((r) => r.session_id !== sessionId))
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not let it back in')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="space-y-5">
      <section className="rounded-xl border border-border bg-card p-5 space-y-3">
        <h2 className="text-sm font-semibold text-foreground">What canopy may learn from your sessions</h2>
        <p className="text-xs text-muted-foreground">
          Each class is your own yes. Nobody can turn one on for you, and turning one off takes effect at once,
          including for what was already derived from your sessions.
        </p>
        {on.has('individual') && managerSeesYou && (
          <div role="note" className="flex items-center justify-between gap-3 rounded-md border border-border p-3 text-xs">
            <span className="text-foreground">
              A manager is set in your workspaces, and a manager sees the outputs of coaching about the people they
              manage (never their transcripts).
            </span>
            <button
              type="button"
              disabled={busy}
              className="text-destructive underline"
              onClick={() => {
                const next = new Set(on)
                next.delete('individual')
                void change(next)
              }}
            >
              Remove individual
            </button>
          </div>
        )}
        <table className="w-full text-sm">
          <thead>
            <Tr head="label">
              <Th>On</Th>
              <Th>Class</Th>
              <Th>What it means</Th>
              <Th>Seen by</Th>
            </Tr>
          </thead>
          <tbody>
            {data.classes.map((c) => (
              <Tr key={c.name}>
                <Td>
                  <input
                    type="checkbox"
                    aria-label={`Allow ${c.name} learning`}
                    checked={c.on}
                    disabled={busy}
                    onChange={(e) => toggle(c.name, e.target.checked)}
                    className="h-4 w-4 rounded border-input text-primary"
                  />
                </Td>
                <Td>{c.name}</Td>
                <Td>{c.summary}</Td>
                <Td tone="muted">{c.seen_by}</Td>
              </Tr>
            ))}
          </tbody>
        </table>
        {data.workspaces.length > 0 && (
          <p className="text-xs text-muted-foreground">Only in: {data.workspaces.join(', ')}.</p>
        )}
        {data.standing_rules.length > 0 && (
          <p className="text-xs text-muted-foreground">
            Standing rules: {data.standing_rules.map((r) => Object.entries(r).map(([k, v]) => `${k}=${v}`).join(' ')).join('; ')}
          </p>
        )}
        {data.directives.length > 0 && (
          <ul className="list-disc pl-5 text-xs text-muted-foreground">
            {data.directives.map((d) => (
              <li key={d}>{d}</li>
            ))}
          </ul>
        )}
        {error && <p className="text-sm text-destructive">{error}</p>}
      </section>

      <section className="rounded-xl border border-border bg-card p-5 space-y-2">
        <h2 className="text-sm font-semibold text-foreground">Who manages you</h2>
        {mine.length === 0 ? (
          <p className="text-xs text-muted-foreground">Nobody is set as your manager in your workspaces.</p>
        ) : (
          <ul className="text-sm text-foreground">
            {mine.map((l) => (
              <li key={`${l.person_id}-${l.manager_id}`}>{l.manager} manages you</li>
            ))}
          </ul>
        )}
      </section>

      <section className="rounded-xl border border-border bg-card p-5 space-y-2">
        <h2 className="text-sm font-semibold text-foreground">Sessions you took out</h2>
        {removed.length === 0 ? (
          <p className="text-xs text-muted-foreground">
            None. To remove one, open it and choose Learning… in its ⋯ menu.
          </p>
        ) : (
          <>
            <p className="text-xs text-muted-foreground">
              Nothing may learn from these. Letting one back in allows it from now on; what was already withdrawn
              stays withdrawn.
            </p>
            <ul className="space-y-1 text-sm text-foreground">
              {removed.map((s) => (
                <li key={s.session_id} className="flex items-center justify-between gap-3">
                  {s.workspace ? (
                    <Link className="min-w-0 truncate underline" to={scopedPath(s.workspace, `/sessions/${s.session_id}`)}>
                      {s.title || s.session_id}
                    </Link>
                  ) : (
                    <span className="min-w-0 truncate">{s.title || s.session_id}</span>
                  )}
                  <button
                    type="button"
                    disabled={busy}
                    className="shrink-0 text-xs underline"
                    onClick={() => void restore(s.session_id)}
                  >
                    Let learning use this again
                  </button>
                </li>
              ))}
            </ul>
          </>
        )}
      </section>

      <section className="rounded-xl border border-border bg-card p-5 space-y-2">
        <h2 className="text-sm font-semibold text-foreground">What was read of your words</h2>
        {days.length === 0 ? (
          <p className="text-xs text-muted-foreground">Nothing has been read yet.</p>
        ) : (
          <table className="w-full text-sm">
            <thead>
              <Tr head="label">
                <Th>Day</Th>
                <Th>Learner</Th>
                <Th align="right">Reads</Th>
                <Th align="right">Sessions</Th>
              </Tr>
            </thead>
            <tbody>
              {days.map((d) => (
                <Tr key={`${d.day}-${d.learner}`}>
                  <Td>{d.day}</Td>
                  <Td>{d.learner}</Td>
                  <Td numeric>{d.reads}</Td>
                  <Td numeric>{d.sources}</Td>
                </Tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
    </div>
  )
}
