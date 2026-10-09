import { useCallback, useEffect, useState } from 'react'

import {
  getMyPerson,
  listMyAudit,
  listMyGrants,
  retractFact,
  revokeMyGrant,
  setMyAgentMemory,
} from '../api/people'
import type { HcpAuditEvent, HcpGrant, PersonFactDetail, PersonMe } from '../api/people'

/** "What agents know about me" — every live fact canopy holds about you, by
 *  workspace; which clients (an agent, over a channel, on a host) may read it,
 *  each revocable; and your audit log of every read and change. You can
 *  retract any fact. Fleet brain v1 (canopy#804), on HCP v1 (hcp_api.py). */
export function PeopleMePage() {
  const [me, setMe] = useState<PersonMe | null>(null)
  const [grants, setGrants] = useState<HcpGrant[]>([])
  const [audit, setAudit] = useState<HcpAuditEvent[]>([])
  const [nextCursor, setNextCursor] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState<number | null>(null)
  const [busyGrant, setBusyGrant] = useState<string | null>(null)
  const [busyMemory, setBusyMemory] = useState(false)

  const load = useCallback(() => {
    getMyPerson()
      .then(setMe)
      .catch((e: Error) => setError(e.message))
    listMyGrants()
      .then(setGrants)
      .catch((e: Error) => setError(e.message))
    listMyAudit()
      .then((page) => {
        setAudit(page.events)
        setNextCursor(page.nextCursor)
      })
      .catch((e: Error) => setError(e.message))
  }, [])

  const moreAudit = async () => {
    if (!nextCursor) return
    try {
      const page = await listMyAudit(nextCursor)
      setAudit((prev) => [...prev, ...page.events])
      setNextCursor(page.nextCursor)
    } catch (e) {
      setError((e as Error).message)
    }
  }

  const onRevoke = async (g: HcpGrant) => {
    if (!confirm(`Revoke ${g.client.name}? It will stop being told anything about you, and canopy will not grant it again unless you do.`)) return
    setBusyGrant(g.grantId)
    try {
      await revokeMyGrant(g.grantId)
      load()
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusyGrant(null)
    }
  }

  useEffect(load, [load])

  const onToggleMemory = async () => {
    if (!me) return
    const next = !me.agent_memory
    if (
      !next &&
      !confirm(
        'Turn agent memory off? Agents will stop being told anything about you and stop recording what they learn. Nothing already recorded is deleted.',
      )
    )
      return
    setBusyMemory(true)
    try {
      await setMyAgentMemory(next)
      load()
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusyMemory(false)
    }
  }

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

  const workspaces = Array.from(new Set(allFacts.map((f) => f.workspace))).sort()

  return (
    <div className="mx-auto max-w-4xl px-4 py-8 space-y-6">
      <div>
        <h1 className="text-2xl font-semibold text-foreground">What agents know about me</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          {me.display_name}
          {me.email ? ` · ${me.email}` : ''}. When you talk to an agent, it is handed the few facts
          relevant to what you asked — only within the workspace they were written in — and can
          look up more while it works. Retract anything that is wrong. A fact marked{' '}
          <em>conflicted</em> is something an agent guessed that contradicts what you said; agents
          are not told it until you correct or retract it.
        </p>
      </div>

      <section
        aria-label="Agent memory"
        className="flex flex-col gap-3 rounded-lg border border-border px-4 py-3 sm:flex-row sm:items-center sm:justify-between"
      >
        <div className="min-w-0">
          <div className="text-sm font-semibold text-foreground">
            Let agents remember things about me
          </div>
          <p className="mt-0.5 text-sm text-muted-foreground">
            {me.agent_memory
              ? 'On: agents you talk to are told what is relevant about you, and record what they learn.'
              : 'Off: no agent is told anything about you or records anything. Nothing below is deleted.'}
          </p>
        </div>
        <button
          type="button"
          role="switch"
          aria-checked={me.agent_memory ?? false}
          onClick={onToggleMemory}
          disabled={busyMemory}
          className={`shrink-0 rounded-md px-3 py-1.5 text-sm font-medium disabled:opacity-50 ${
            me.agent_memory
              ? 'bg-primary text-primary-foreground hover:bg-primary/90'
              : 'border border-border text-foreground hover:bg-muted'
          }`}
        >
          {busyMemory ? 'Saving…' : me.agent_memory ? 'On' : 'Off'}
        </button>
      </section>

      {workspaces.length === 0 && (
        <p className="text-muted-foreground">Nothing yet — no agent has recorded anything about you.</p>
      )}

      {workspaces.map((ws) => {
        const facts = allFacts.filter((f) => f.workspace === ws)
        return (
          <section key={ws} className="rounded-lg border border-border">
            <h2 className="border-b border-border px-4 py-2 text-sm font-semibold text-foreground">
              {ws}
            </h2>
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
                        {f.status && f.status !== 'active' ? (
                          <span className="mr-1 rounded bg-destructive/10 px-1 text-destructive">
                            {f.status}
                          </span>
                        ) : null}
                        {f.basis}
                        {f.confidence ? ` (${f.confidence} confidence)` : ''}
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
        <h2 className="text-sm font-semibold text-foreground">Who can read it</h2>
        <p className="mt-1 text-xs text-muted-foreground">
          Each agent is allowed separately for each way you reach it — ACE over Slack and ACE over
          email are two entries. canopy allows an agent the first time it serves you; revoking stops
          it immediately and for good, unless you allow it again.
        </p>
        {grants.length === 0 ? (
          <p className="mt-2 text-sm text-muted-foreground">No agent has served you yet.</p>
        ) : (
          <ul className="mt-2 divide-y divide-border rounded-lg border border-border text-sm">
            {grants.map((g) => (
              <li key={g.grantId} className="flex items-start gap-3 px-4 py-2">
                <div className="min-w-0 flex-1">
                  <div className="text-foreground">{g.client.name}</div>
                  <div className="mt-0.5 text-xs text-muted-foreground">
                    {g.status}
                    {` · ${g.canopy.workspace}`}
                    {` · since ${new Date(g.issuedAt).toLocaleDateString()}`}
                    {g.expiresAt ? ` · until ${new Date(g.expiresAt).toLocaleString()}` : ''}
                    {` · ${Array.from(new Set(g.scopes.map((s) => s.split(':').slice(1, -1).join(':')))).join(', ')}`}
                  </div>
                </div>
                {g.status === 'active' && (
                  <button
                    className="shrink-0 text-destructive hover:text-destructive/80 disabled:opacity-50"
                    disabled={busyGrant === g.grantId}
                    onClick={() => onRevoke(g)}
                  >
                    Revoke
                  </button>
                )}
              </li>
            ))}
          </ul>
        )}
      </section>

      <section>
        <h2 className="text-sm font-semibold text-foreground">Audit log</h2>
        <p className="mt-1 text-xs text-muted-foreground">
          Every read and change of what canopy holds about you, newest first. Only you can see it.
        </p>
        {audit.length === 0 ? (
          <p className="mt-2 text-sm text-muted-foreground">Nothing yet.</p>
        ) : (
          <ul className="mt-2 divide-y divide-border rounded-lg border border-border text-sm">
            {audit.map((e) => (
              <li key={e.eventId} className="flex flex-wrap gap-x-3 px-4 py-2">
                <span className="text-muted-foreground">{new Date(e.timestamp).toLocaleString()}</span>
                <span className="text-foreground">{e.actorId}</span>
                <span className="text-muted-foreground">
                  {e.eventType}
                  {e.category ? ` · ${e.category}` : ''}
                  {e.purpose ? ` · “${e.purpose}”` : ''}
                </span>
              </li>
            ))}
          </ul>
        )}
        {nextCursor && (
          <button className="mt-2 text-sm text-primary hover:underline" onClick={moreAudit}>
            Show older
          </button>
        )}
      </section>
    </div>
  )
}
