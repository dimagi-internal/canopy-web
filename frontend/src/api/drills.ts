// Readiness-drill API client — a runner-owner-gated fan-out of pinned doctor
// turns per agent, so a runner's owner can confirm it can actually claim work
// for the agents assigned to it before relying on it. Separate from ./harness
// (the runner registry) to keep the drill surface distinct and easy to find.
import { apiV2 } from './client.v2'
import type { components } from './generated'

export type RunnerDrillOut = components['schemas']['RunnerDrillOut']

function unwrap<T>(res: { data?: T; error?: unknown }, what: string): T {
  if (res.error !== undefined || res.data === undefined) {
    throw new Error(`${what} failed: ${JSON.stringify(res.error ?? 'no data')}`)
  }
  return res.data
}

// Fan out a readiness drill for this runner. Default (omitted/empty `agents`)
// drills every agent assigned to the runner; the server returns one
// RunnerDrillOut per (runner, agent) pair drilled.
export async function startDrill(
  runnerId: string,
  agents?: readonly string[],
): Promise<RunnerDrillOut[]> {
  const res = await apiV2.POST('/api/harness/runners/{runner_id}/drill', {
    params: { path: { runner_id: runnerId } },
    body: { agents: agents ? [...agents] : null },
  })
  // openapi-fetch's Readable<T> helper degrades `readonly Foo[]` into an
  // ArrayLike-shaped object (numeric index + length, no Symbol.iterator) — see
  // ./agents.ts's toPage comment for the full explanation. Array.from rebuilds
  // a real array rather than casting.
  return Array.from(unwrap(res, 'startDrill'))
}

// List this runner's drill outcomes across all its (runner, agent) pairs.
export async function listDrills(runnerId: string): Promise<RunnerDrillOut[]> {
  const res = await apiV2.GET('/api/harness/runners/{runner_id}/drills', {
    params: { path: { runner_id: runnerId } },
  })
  return Array.from(unwrap(res, 'listDrills'))
}
