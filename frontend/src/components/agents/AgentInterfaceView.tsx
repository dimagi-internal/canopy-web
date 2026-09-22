import { useEffect, useState } from 'react'
import { getAgentInterface, type AgentInterfaceOut } from '@/api/agents'

type Capability = {
  description?: string
  callers?: string[]
  entry?: string | null
  tools?: string[]
  bash?: string[]
  input?: Record<string, string>
}

// Read-only: the interface lives in the agent's repo (config/interface.yaml) and is
// published with `canopy agent interface`. Shown so that "what can someone outside
// reach through this agent?" has an answer on the agent's own page.
export function AgentInterfaceView({ agentSlug }: { agentSlug: string }) {
  const [data, setData] = useState<AgentInterfaceOut | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    getAgentInterface(agentSlug)
      .then((d) => !cancelled && setData(d))
      .catch((e: unknown) => !cancelled && setError(e instanceof Error ? e.message : 'Could not load'))
    return () => {
      cancelled = true
    }
  }, [agentSlug])

  if (error) return <p role="alert" className="m-0 text-[12px] text-destructive">{error}</p>
  if (data === null) return <p className="m-0 text-[12px] text-muted-foreground">Loading…</p>

  const caps = ((data.interface as { capabilities?: Record<string, Capability> })?.capabilities) ?? {}
  const names = Object.keys(caps)
  if (names.length === 0) {
    return (
      <p data-testid="interface-none" className="m-0 text-[13px] text-foreground-secondary">
        Not published. Everyone who can reach this agent gets all of it.
      </p>
    )
  }
  return (
    <div className="flex flex-col gap-2">
      <table className="w-full text-[13px]">
        <thead>
          <tr className="text-left text-[11px] text-muted-foreground">
            <th className="py-1 font-medium">Capability</th>
            <th className="py-1 font-medium">Who</th>
            <th className="py-1 font-medium">Allows</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-border">
          {names.map((n) => {
            const c = caps[n]
            return (
              <tr key={n} data-testid="interface-capability">
                <td className="py-1.5 align-top text-foreground">
                  <span className="font-mono">{n}</span>
                  {c.description && <div className="text-[12px] text-muted-foreground">{c.description}</div>}
                  <div className="text-[11px] text-muted-foreground">
                    MCP: <span className="font-mono">{`${agentSlug}__${n}`}</span>
                    {Object.keys(c.input ?? {}).length > 0 &&
                      ` (${Object.entries(c.input ?? {}).map(([k, t]) => `${k}: ${t}`).join(', ')})`}
                  </div>
                </td>
                <td className="py-1.5 align-top text-[12px] text-foreground-secondary">
                  {(c.callers ?? []).join(', ') || 'nobody'}
                </td>
                <td className="py-1.5 align-top font-mono text-[11px] text-foreground-secondary">
                  {[...(c.tools ?? []), ...(c.bash ?? [])].map((t) => (
                    <div key={t}>{t}</div>
                  ))}
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
      {data.published_at && (
        <p className="m-0 text-[11px] text-muted-foreground">
          Published {new Date(data.published_at).toLocaleString()}
          {data.published_by_email ? ` by ${data.published_by_email}` : ''}. Everything not listed is refused.
        </p>
      )}
    </div>
  )
}
