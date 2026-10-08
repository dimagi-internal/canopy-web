import { useCallback, useEffect, useState } from 'react'

import { getMyPerson, retractFact } from '../api/people'
import type { PersonFactDetail, PersonMe } from '../api/people'

/** "What agents know about me" — every live fact canopy holds about you, by
 *  workspace, the digest agents read, and every recent read of it. You can
 *  retract any fact. Fleet brain v1 (canopy#804). */
export function PeopleMePage() {
  const [me, setMe] = useState<PersonMe | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState<number | null>(null)

  const load = useCallback(() => {
    getMyPerson()
      .then(setMe)
      .catch((e: Error) => setError(e.message))
  }, [])

  useEffect(load, [load])

  const onRetract = async (fact: PersonFactDetail) => {
    if (!me) return
    if (!confirm(`Retract "${fact.statement}"? Agents will stop being told it.`)) return
    setBusy(fact.id)
    try {
      await retractFact(me.id, fact.id)
      load()
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy(null)
    }
  }

  if (error) return <div className="p-6 text-destructive">{error}</div>
  if (me === null) return <div className="p-6 text-muted-foreground">Loading…</div>

  const allFacts = me.facts ?? []
  const digests = me.digests ?? []
  const accesses = me.accesses ?? []

  const workspaces = Array.from(
    new Set([...allFacts.map((f) => f.workspace), ...digests.map((d) => d.workspace)]),
  ).sort()

  return (
    <div className="mx-auto max-w-4xl px-4 py-8 space-y-6">
      <div>
        <h1 className="text-2xl font-semibold text-foreground">What agents know about me</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          {me.display_name}
          {me.email ? ` · ${me.email}` : ''}. Agents are handed these facts and the digest when
          you talk to them, only within the workspace they were written in. Retract anything that
          is wrong.
        </p>
      </div>

      {workspaces.length === 0 && (
        <p className="text-muted-foreground">Nothing yet — no agent has recorded anything about you.</p>
      )}

      {workspaces.map((ws) => {
        const facts = allFacts.filter((f) => f.workspace === ws)
        const digest = digests.find((d) => d.workspace === ws)
        return (
          <section key={ws} className="rounded-lg border border-border">
            <h2 className="border-b border-border px-4 py-2 text-sm font-semibold text-foreground">
              {ws}
            </h2>
            {digest && digest.text && (
              <div className="border-b border-border px-4 py-3 text-sm">
                <div className="text-xs uppercase tracking-wider text-muted-foreground">Digest</div>
                <p className="mt-1 whitespace-pre-wrap text-foreground">{digest.text}</p>
                <div className="mt-1 text-xs text-muted-foreground">
                  {digest.updated_by ? `by ${digest.updated_by} · ` : ''}
                  {digest.updated_at ? new Date(digest.updated_at).toLocaleString() : ''}
                </div>
              </div>
            )}
            {facts.length === 0 ? (
              <p className="px-4 py-3 text-sm text-muted-foreground">No facts.</p>
            ) : (
              <ul className="divide-y divide-border">
                {facts.map((f) => (
                  <li key={f.id} className="flex items-start gap-3 px-4 py-3 text-sm">
                    <span className="mt-0.5 shrink-0 rounded-full bg-muted px-2 py-0.5 text-xs text-muted-foreground">
                      {f.kind}
                    </span>
                    <div className="min-w-0 flex-1">
                      <div className="text-foreground">{f.statement}</div>
                      <div className="mt-0.5 text-xs text-muted-foreground">
                        {f.basis}
                        {f.asserted_by ? ` · by ${f.asserted_by}` : ''}
                        {f.project ? ` · ${f.project.title}` : ''}
                        {f.instance_ref ? ` · ${f.instance_ref}` : ''}
                        {f.created_at ? ` · ${new Date(f.created_at).toLocaleDateString()}` : ''}
                      </div>
                    </div>
                    <button
                      className="shrink-0 text-destructive hover:text-destructive/80 disabled:opacity-50"
                      disabled={busy === f.id}
                      onClick={() => onRetract(f)}
                    >
                      Retract
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </section>
        )
      })}

      <section>
        <h2 className="text-sm font-semibold text-foreground">Recent reads</h2>
        {accesses.length === 0 ? (
          <p className="mt-2 text-sm text-muted-foreground">Nobody has read it yet.</p>
        ) : (
          <ul className="mt-2 divide-y divide-border rounded-lg border border-border text-sm">
            {accesses.map((a, i) => (
              <li key={i} className="flex flex-wrap gap-x-3 px-4 py-2">
                <span className="text-muted-foreground">{new Date(a.created_at).toLocaleString()}</span>
                <span className="text-foreground">
                  {a.reader_agent ?? a.reader_user ?? 'someone'}
                </span>
                <span className="text-muted-foreground">
                  {a.via === 'envelope' ? 'in a turn' : 'looked it up'}
                  {a.workspace ? ` · ${a.workspace}` : ''}
                </span>
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  )
}
