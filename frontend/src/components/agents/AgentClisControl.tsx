import { useState } from 'react'

import { setAgentClis } from '@/api/agents'
import { CLIS, cliLabel } from '@/lib/clis'

/** Which agent CLIs the agent's turns run on, preferred first, and the model each
 *  starts on. A turn runs on the first one its runner has signed in and can confine
 *  it on; a conversation keeps the one it started on (`apps/harness/clis.py`). */
export function AgentClisControl({ slug, initialClis, initialModels, canEdit }: {
  slug: string
  initialClis: readonly string[]
  initialModels: Readonly<Record<string, string>>
  canEdit: boolean
}) {
  const [saved, setSaved] = useState({ clis: [...initialClis], models: { ...initialModels } })
  const [order, setOrder] = useState<string[]>([...initialClis])
  const [models, setModels] = useState<Record<string, string>>({ ...initialModels })
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [done, setDone] = useState(false)

  const cleanModels = Object.fromEntries(
    Object.entries(models).map(([k, v]) => [k, v.trim()]).filter(([k, v]) => v && order.includes(k)),
  )
  const dirty = order.join() !== saved.clis.join() || JSON.stringify(cleanModels) !== JSON.stringify(saved.models)
  const touch = () => setDone(false)
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
      const a = await setAgentClis(slug, order, cleanModels)
      setSaved({ clis: a.clis, models: a.cli_models })
      setOrder(a.clis)
      setModels(a.cli_models)
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
        {saved.clis.map((cli, i) => (
          <li key={cli}>
            {i + 1}. {cliLabel(cli)}
            {saved.models[cli] && <span className="ml-2 font-mono text-[12px] text-muted-foreground">{saved.models[cli]}</span>}
          </li>
        ))}
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
            <th className="py-1 font-normal">Model (blank = its default)</th>
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
                <td className="py-1.5">
                  <input value={models[cli] ?? ''} disabled={!on} aria-label={`${cliLabel(cli)} model`}
                         onChange={(e) => { touch(); setModels((m) => ({ ...m, [cli]: e.target.value })) }}
                         className="min-h-8 w-48 rounded-md border border-border bg-input px-2 font-mono text-[12px] disabled:opacity-50" />
                </td>
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
        {done && <span className="text-[12px] text-success">Saved. New turns follow it; a conversation keeps the CLI it started on.</span>}
        {error && <span className="text-[12px] text-destructive">{error}</span>}
      </div>
    </div>
  )
}
