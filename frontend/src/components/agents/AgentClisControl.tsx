import { useState } from 'react'

import { setAgentClis } from '@/api/agents'
import { CLIS, type Cli, cliLabel } from '@/lib/clis'

/** Which agent CLIs the agent is set up for, its default first. A new chat or
 *  schedule may pick any of them; one that picks none runs on the first its runner
 *  has signed in and can confine it on, and a conversation keeps the one it runs
 *  on (`apps/harness/clis.py`). The server refuses a CLI no runner here has, and
 *  taking off one a schedule still runs on — its reason is shown as it comes. */
export function AgentClisControl({ slug, initialClis, canEdit }: {
  slug: string
  initialClis: readonly string[]
  canEdit: boolean
}) {
  const [saved, setSaved] = useState<string[]>([...initialClis])
  const [order, setOrder] = useState<string[]>([...initialClis])
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [done, setDone] = useState(false)

  const dirty = order.join() !== saved.join()
  const touch = () => { setDone(false); setError('') }
  const toggle = (cli: string) => {
    touch()
    setOrder((o) => (o.includes(cli) ? o.filter((c) => c !== cli) : [...o, cli]))
  }
  const prefer = (cli: string) => {
    touch()
    setOrder((o) => [cli, ...o.filter((c) => c !== cli)])
  }

  const save = async () => {
    if (!dirty || busy || order.length === 0) return
    setBusy(true)
    setError('')
    try {
      const a = await setAgentClis(slug, order as Cli[])
      setSaved(a.clis)
      setOrder(a.clis)
      setDone(true)
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : 'Could not save the agent CLIs')
    } finally {
      setBusy(false)
    }
  }

  if (!canEdit) {
    return (
      <ol className="text-[13px] text-foreground" data-testid="agent-clis">
        {saved.map((cli, i) => <li key={cli}>{i + 1}. {cliLabel(cli)}</li>)}
      </ol>
    )
  }
  const rows = [...order, ...CLIS.filter((c) => !order.includes(c))]
  return (
    <div data-testid="agent-clis">
      <table className="w-full text-[13px]">
        <thead>
          <tr className="text-left text-[11px] uppercase tracking-wide text-muted-foreground">
            <th className="w-8 py-1 font-normal">On</th>
            <th className="py-1 font-normal">CLI</th>
            <th className="py-1" />
          </tr>
        </thead>
        <tbody>
          {rows.map((cli) => {
            const on = order.includes(cli)
            const rank = order.indexOf(cli)
            return (
              <tr key={cli} className="border-t border-border">
                <td className="py-1.5">
                  <input type="checkbox" checked={on} onChange={() => toggle(cli)} aria-label={`Run on ${cliLabel(cli)}`} />
                </td>
                <td className="py-1.5 text-foreground">{on ? `${rank + 1}. ` : ''}{cliLabel(cli)}</td>
                <td className="py-1.5 text-right">
                  {on && rank > 0 && (
                    <button type="button" onClick={() => prefer(cli)}
                            className="rounded border border-border px-2 py-0.5 text-[12px] text-foreground hover:bg-muted">
                      Prefer {cliLabel(cli)}
                    </button>
                  )}
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
      <div className="mt-2 flex items-center gap-2">
        <button type="button" onClick={save} disabled={!dirty || busy || order.length === 0}
                className="min-h-8 rounded-md border border-border px-3 text-[13px] text-foreground disabled:opacity-50">
          {busy ? 'Saving…' : 'Save'}
        </button>
        {done && <span className="text-[12px] text-success">Saved. New turns follow it; a conversation keeps the CLI it runs on.</span>}
        {error && <span className="text-[12px] text-destructive" data-testid="agent-clis-error">{error}</span>}
      </div>
    </div>
  )
}
