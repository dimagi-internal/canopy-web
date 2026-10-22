import { useCallback, useEffect, useMemo, useState } from 'react'

import {
  addMyEntry,
  correctMyEntry,
  getMyPerson,
  getPersonPage,
  listMyAudit,
  listMyCollections,
  listMyGrants,
  listPersonPageAudit,
  retractFact,
  revokeMyGrant,
  setMyAgentMemory,
} from '@/api/people'
import type { HcpAuditEvent, HcpGrant, MemoryCollections, PersonFactDetail, PersonMe } from '@/api/people'
import { listWorkspaces, type WorkspaceOut } from '@/api/workspaces'
import { activeAccess, agentName, type AgentAccess } from './words'

/** A person's memory page: their record and its entries, the grants that let
 *  agents read it, the audit log, and (for your own page) your collections and
 *  workspaces — plus every write the page makes, each re-reading the page after.
 *
 *  `viewing` is someone else's page as they let this agent see it: read-only,
 *  fed by the viewer routes. `fatal` means the record itself didn't load (there
 *  is no page); `error` is a section or an action that failed, shown as a
 *  banner. Destructive writes confirm first, as the page always has. */
export function usePersonFacts(viewing?: number) {
  const [me, setMe] = useState<PersonMe | null>(null)
  const [grants, setGrants] = useState<HcpGrant[]>([])
  const [audit, setAudit] = useState<HcpAuditEvent[]>([])
  const [nextCursor, setNextCursor] = useState<string | null>(null)
  const [collections, setCollections] = useState<MemoryCollections | null>(null)
  const [workspaces, setWorkspaces] = useState<WorkspaceOut[]>([])
  const [fatal, setFatal] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  /** The entry (fact id) a write is in flight on. */
  const [busy, setBusy] = useState<number | null>(null)
  /** The agent (access key) whose access is being taken back. */
  const [busyGrant, setBusyGrant] = useState<string | null>(null)
  /** The memory switch being changed, as `<record|use>:<available|default>`. */
  const [busyMemory, setBusyMemory] = useState<string | null>(null)
  const [busyCollections, setBusyCollections] = useState(false)

  const load = useCallback(() => {
    if (viewing !== undefined) {
      getPersonPage(viewing)
        .then((page) => {
          setMe(page.me)
          setGrants(page.grants as unknown as HcpGrant[])
          setCollections(page.collections)
        })
        .catch((e: Error) => setFatal(e.message))
      listPersonPageAudit(viewing)
        .then((page) => {
          setAudit(page.events)
          setNextCursor(page.nextCursor)
        })
        .catch((e: Error) => setError(`Couldn't load what happened: ${e.message}`))
      return
    }
    // Collections and your workspaces only feed controls; if either fails the
    // rest of the page still stands.
    listMyCollections()
      .then(setCollections)
      .catch(() => setCollections(null))
    listWorkspaces()
      .then(setWorkspaces)
      .catch(() => setWorkspaces([]))
    getMyPerson()
      .then(setMe)
      .catch((e: Error) => setFatal(e.message))
    listMyGrants()
      .then(setGrants)
      .catch((e: Error) => setError(`Couldn't load which agents can read your memory: ${e.message}`))
    listMyAudit()
      .then((page) => {
        setAudit(page.events)
        setNextCursor(page.nextCursor)
      })
      .catch((e: Error) => setError(`Couldn't load what happened: ${e.message}`))
  }, [viewing])

  useEffect(load, [load])

  // agent slug → the name you know it by, from your grants ("ace" → "ACE").
  const names = useMemo(() => {
    const m = new Map<string, string>()
    for (const g of grants) if (g.canopy.agent) m.set(g.canopy.agent, agentName(g))
    return m
  }, [grants])
  const access = useMemo(() => activeAccess(grants), [grants])
  // Reads are shown per conversation; this log keeps what changed and who looked.
  const changes = useMemo(() => audit.filter((e) => e.eventType !== 'preference.read'), [audit])
  const past = useMemo(() => grants.filter((g) => g.status !== 'active'), [grants])

  const run = async <K,>(mark: (v: K | null) => void, key: K, work: () => Promise<unknown>) => {
    mark(key)
    try {
      await work()
      load()
    } catch (e) {
      setError((e as Error).message)
    } finally {
      mark(null)
    }
  }

  const revoke = (a: AgentAccess) => {
    if (!confirm(`Take back ${a.name}'s access? It stops being told anything about you right away, and won't be again unless you allow it.`)) return
    void run(setBusyGrant, a.key, () => Promise.all(a.grants.map((g) => revokeMyGrant(g.grantId))))
  }

  const toggleAvailable = (which: 'record' | 'use') => {
    if (!me) return
    const next = !me.agent_memory[which].available
    const offWarning =
      which === 'record'
        ? 'Stop agents learning from you, in every conversation? Nothing already remembered is deleted.'
        : 'Stop agents using your memory, in every conversation? Nothing is deleted.'
    if (!next && !confirm(offWarning)) return
    void run(setBusyMemory, `${which}:available`, () => setMyAgentMemory({ [which]: { available: next } }))
  }

  const toggleDefault = (which: 'record' | 'use') => {
    if (!me) return
    void run(setBusyMemory, `${which}:default`, () =>
      setMyAgentMemory({ [which]: { default: !me.agent_memory[which].default } }),
    )
  }

  const retract = (fact: PersonFactDetail) => {
    if (!me) return
    if (!confirm(`Forget "${fact.statement}"? Agents stop being told it.`)) return
    void run(setBusy, fact.id, () => retractFact(me.id, fact.id))
  }

  /** Replace an entry's text. A blank or unchanged text is a no-op. */
  const correct = (fact: PersonFactDetail, text: string) => {
    const next = text.trim()
    if (!fact.entry_id || !next || next === fact.statement) return
    void run(setBusy, fact.id, () => correctMyEntry(fact.entry_id as string, next))
  }

  /** Add an entry; resolves whether it landed (the composer clears on true). */
  const add = async (category: string, text: string, workspace: string | null) => {
    try {
      await addMyEntry(category, text, workspace)
      load()
      return true
    } catch (err) {
      setError((err as Error).message)
      return false
    }
  }

  // A collection change re-reads grants too: a grant's reach follows its collection.
  const actOnCollections = (work: () => Promise<unknown>) => {
    setBusyCollections(true)
    work()
      .then(load)
      .catch((e: Error) => setError(e.message))
      .finally(() => setBusyCollections(false))
  }

  /** Fetch the next audit page; resolves whether there was one. */
  const loadMoreAudit = async (): Promise<boolean> => {
    if (!nextCursor) return false
    try {
      const page = viewing !== undefined ? await listPersonPageAudit(viewing, nextCursor) : await listMyAudit(nextCursor)
      setAudit((prev) => [...prev, ...page.events])
      setNextCursor(page.nextCursor)
      return true
    } catch (e) {
      setError((e as Error).message)
      return false
    }
  }

  return {
    me,
    grants,
    audit,
    nextCursor,
    collections,
    workspaces,
    fatal,
    error,
    setError,
    busy,
    busyGrant,
    busyMemory,
    busyCollections,
    names,
    access,
    changes,
    past,
    load,
    revoke,
    toggleAvailable,
    toggleDefault,
    retract,
    correct,
    add,
    actOnCollections,
    loadMoreAudit,
  }
}
