import { useEffect, useState } from 'react'

import { getWorkspaceHealthSetup, type AgentSetupHealthOut } from '@/api/workspaces'

/** Agents per read, and reads in flight: each agent's checklist costs a few dozen
 *  queries on the server, so a few at a time fill the table top to bottom while
 *  no one read holds it up. */
export const SETUP_BATCH = 3
export const SETUP_PARALLEL = 4

/** Each agent's checklist and vault state, read after the table is up
 *  (`GET /health/setup`), in the order given. `failed` holds the slugs whose read
 *  failed; `done` is true once every slug has an answer or failed. Starts over
 *  when `key` changes. */
export function useAgentSetup(slug: string, agents: readonly string[] | null, key: string): {
  setup: Record<string, AgentSetupHealthOut>
  failed: ReadonlySet<string>
  done: boolean
} {
  const [state, setState] = useState<{ key: string; setup: Record<string, AgentSetupHealthOut>; failed: Set<string> }>(
    { key: '', setup: {}, failed: new Set() })

  useEffect(() => {
    if (!agents) return
    let off = false
    setState({ key, setup: {}, failed: new Set() })
    const batches: string[][] = []
    for (let i = 0; i < agents.length; i += SETUP_BATCH) batches.push(agents.slice(i, i + SETUP_BATCH))
    const next = async (): Promise<void> => {
      for (let batch = batches.shift(); batch && !off; batch = batches.shift()) {
        const names = batch
        try {
          const rows = await getWorkspaceHealthSetup(slug, names)
          if (off) return
          // One the server left out (gone since the table was read) has no answer coming.
          const missing = names.filter((n) => !rows.some((r) => r.slug === n))
          setState((s) => ({
            ...s,
            setup: { ...s.setup, ...Object.fromEntries(rows.map((r) => [r.slug, r])) },
            failed: missing.length ? new Set([...s.failed, ...missing]) : s.failed,
          }))
        } catch {
          if (off) return
          setState((s) => ({ ...s, failed: new Set([...s.failed, ...names]) }))
        }
      }
    }
    for (let i = 0; i < SETUP_PARALLEL; i++) void next()
    return () => { off = true }
    // `agents` arrives with the table; `key` names it.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, agents === null])

  if (state.key !== key || !agents) return { setup: {}, failed: new Set(), done: false }
  return {
    setup: state.setup,
    failed: state.failed,
    done: agents.every((a) => a in state.setup || state.failed.has(a)),
  }
}
