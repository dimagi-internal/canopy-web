import { enforceEvents } from '@ag-ui/client'
import type { BaseEvent } from '@ag-ui/core'
import { from, lastValueFrom, toArray } from 'rxjs'
import { describe, expect, it, vi } from 'vitest'

import fixture from '../../packages/canopy-ui/src/chat/agui.fixture.json'

/**
 * Does a real AG-UI 1.0 client keep everything canopy sends?
 *
 * The rest of canopy's AG-UI suite checks canopy against ITSELF: Python
 * projects, TypeScript inverts, the fixture ties them together. None of that
 * can say whether a third party would accept the events, which is the reason to
 * speak a standard. So this runs the fixture — generated from the server's own
 * projection — through `enforceEvents`, the stage every AG-UI 1.0 client runs on
 * incoming events.
 *
 * Validation alone would prove too little. `enforceEvents` does not reject an
 * unknown field, it STRIPS it with a warning — and the zod schemas in
 * `@ag-ui/core` pass unknown fields through, so a schema check here passed even
 * for a field a real client would delete (found by trying it). canopy's
 * lossy-frame originals ride under `metadata.canopy`; if that ever stopped
 * being legal, every schema check would stay green while 1.0 clients quietly
 * lost them. Hence: the output must EQUAL the input, and nothing may warn.
 *
 * What this does NOT prove, stated so nobody infers it: that a stock AG-UI
 * agent client can consume canopy's session socket. It cannot — `verifyEvents`
 * requires a stream to open with RUN_STARTED, because AG-UI's transport is one
 * request = one run over SSE, while canopy's socket is a long-lived multiplayer
 * session. The events are AG-UI 1.0; the transport is canopy's. Serving a
 * standard run endpoint is its own piece of work.
 *
 * `@ag-ui/client` and `@ag-ui/core` are devDependencies on purpose: the oracle,
 * never the implementation. canopy builds AG-UI in `apps/canopy_sessions/agui.py`
 * and reads it in `canopy-ui/src/chat/agui.ts`, and neither imports an SDK.
 */

type Entry = { canopy: { event: string }; agui: Array<Record<string, unknown>> }
const entries = fixture as unknown as Entry[]
const events = entries.flatMap((entry) =>
  entry.agui.map((event) => [entry.canopy.event, event.type as string, event] as const),
)

async function throughA1_0Client(input: Record<string, unknown>[]): Promise<BaseEvent[]> {
  // Cast at the boundary on purpose: the point is to hand the client exactly
  // the JSON the server sent, not something the type system has tidied up.
  return lastValueFrom(from(input as unknown as BaseEvent[]).pipe(enforceEvents(), toArray()))
}

describe('an AG-UI 1.0 client keeps every event canopy sends', () => {
  it('has something to check', () => {
    // An empty fixture would make every assertion below vacuously true.
    expect(events.length).toBeGreaterThanOrEqual(20)
  })

  it.each(events)('%s → %s passes enforcement untouched', async (_canopy, _type, event) => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    try {
      const [out] = await throughA1_0Client([event])

      expect(out).toEqual(event)
      expect(warn, JSON.stringify(warn.mock.calls)).not.toHaveBeenCalled()
    } finally {
      warn.mockRestore()
    }
  })

  it('would notice a field a 1.0 client strips', async () => {
    // The check above is only worth something if enforcement really strips.
    // If a future release stopped stripping, `toEqual` would pass for the wrong
    // reason — so pin the behaviour the test depends on.
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    const [out] = await throughA1_0Client([{ type: 'CUSTOM', name: 'canopy.x', value: 1, notInSchema: true }])
    const warned = warn.mock.calls.length
    warn.mockRestore()

    expect(out).not.toHaveProperty('notInSchema')
    // And it says so — which is what makes "nothing warned" above a real check.
    expect(warned).toBeGreaterThan(0)
  })
})
