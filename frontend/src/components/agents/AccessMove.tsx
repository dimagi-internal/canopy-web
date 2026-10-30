import { useState } from 'react'

import { applyAccessConversion, previewAccessConversion, type AccessConversionOut } from '@/api/agents'
import { roleWord } from './accessWords'

// MOVING AN AGENT ONTO ITS ACCESS LIST.
//
// Until an agent moves, the rules it was published with (see Capabilities) still let people in
// beside this list. A preview translates them into lines and asks the access rule the old
// way and the new way about everyone who could plausibly ask; the move is offered only when
// nobody's access changes, and the server refuses it otherwise.
export function AccessMove({
  agentSlug,
  agentName,
  canEdit,
  onMoved,
}: {
  agentSlug: string
  agentName: string
  canEdit: boolean
  onMoved: () => Promise<void> | void
}) {
  const [plan, setPlan] = useState<AccessConversionOut | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const run = async (fn: () => Promise<void>) => {
    setBusy(true)
    setError(null)
    try {
      await fn()
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'That did not work')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="rounded-lg border border-border bg-muted/40 p-3 text-[12px]" data-testid="access-move">
      <p className="m-0 text-muted-foreground">
        {agentName} has not moved onto this list yet: the rules it published before (see Capabilities) still let people in
        too. A line here adds to them.
      </p>
      {canEdit && plan === null && (
        <button
          type="button"
          disabled={busy}
          onClick={() => void run(async () => setPlan(await previewAccessConversion(agentSlug)))}
          className="mt-2 rounded border border-input px-2 py-0.5 text-foreground hover:bg-muted"
        >
          See what moving would do
        </button>
      )}
      {plan && (
        <div className="mt-2" data-testid="access-move-plan">
          {plan.ok ? (
            <p className="m-0 text-foreground">
              Moving changes nobody&rsquo;s access ({plan.probed} checks, each answered the same before and after). It
              would put these on the list:
            </p>
          ) : (
            <p role="status" className="m-0 text-warning">
              Moving would change someone&rsquo;s access, so it is not offered yet:
            </p>
          )}
          <ul className="m-0 mt-1 list-disc pl-5 text-foreground-secondary">
            {plan.ok &&
              plan.lines.map((l) => (
                <li key={`${l.who}:${l.role}`}>
                  {l.who}: {roleWord(l.role).toLowerCase()}
                  {l.manual_only ? ', needs approval before anything goes out' : ''}
                </li>
              ))}
            {!plan.ok && [...plan.unsupported, ...plan.differences.slice(0, 5)].map((d) => <li key={d}>{d}</li>)}
          </ul>
          {plan.ok && (
            <button
              type="button"
              disabled={busy}
              onClick={() =>
                void run(async () => {
                  await applyAccessConversion(agentSlug)
                  await onMoved()
                })
              }
              className="mt-2 rounded border border-primary bg-primary px-2 py-0.5 text-primary-foreground hover:opacity-90"
            >
              Move {agentName} onto the access list
            </button>
          )}
        </div>
      )}
      {error && (
        <p role="alert" className="m-0 mt-2 text-destructive">
          {error}
        </p>
      )}
    </div>
  )
}
