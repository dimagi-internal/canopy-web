import { useEffect, useState } from 'react'
import {
  getAgentInterface,
  saveAgentInterface,
  unpublishAgentInterface,
  type AgentInterfaceOut,
} from '@/api/agents'

type Capability = {
  description?: string
  callers?: string[]
  entry?: string | null
  tools?: string[]
  bash?: string[]
  input?: Record<string, string>
}

const STARTER = `# Who gets the WHOLE agent, as its admins do (domain-wide access).
# Classes: member | contact | unknown, optionally @domain.tld, optionally :verified
full:
  - contact@example.org:verified

# What everyone else may ask for. Everything not listed is refused.
capabilities:
  ask:
    description: Ask a question by email.
    callers: [contact]
callers_default: none
`

// The agent's declared interface: who may make it do what. LIVE STATE held by
// canopy-web — not a file in the agent's repo — so owners and admins edit it
// here, and every member can read it.
export function AgentInterfaceView({ agentSlug, canEdit = false }: { agentSlug: string; canEdit?: boolean }) {
  const [data, setData] = useState<AgentInterfaceOut | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [draft, setDraft] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    let cancelled = false
    getAgentInterface(agentSlug)
      .then((d) => !cancelled && setData(d))
      .catch((e: unknown) => !cancelled && setError(e instanceof Error ? e.message : 'Could not load'))
    return () => {
      cancelled = true
    }
  }, [agentSlug])

  const run = async (fn: () => Promise<AgentInterfaceOut>) => {
    setBusy(true)
    setError(null)
    try {
      setData(await fn())
      setDraft(null)
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Could not save')
    } finally {
      setBusy(false)
    }
  }

  const startEdit = () => {
    const iface = data?.interface ?? {}
    // JSON is valid YAML, so an interface saved as a mapping still round-trips.
    setDraft(data?.source || (Object.keys(iface).length ? JSON.stringify(iface, null, 2) : STARTER))
  }

  if (data === null) {
    return error
      ? <p role="alert" className="m-0 text-[12px] text-destructive">{error}</p>
      : <p className="m-0 text-[12px] text-muted-foreground">Loading…</p>
  }

  if (draft !== null) {
    return (
      <div className="flex flex-col gap-2">
        <label htmlFor={`iface-${agentSlug}`} className="text-[12px] text-muted-foreground">
          Interface (YAML)
        </label>
        <textarea
          id={`iface-${agentSlug}`}
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          rows={Math.min(30, Math.max(10, draft.split('\n').length + 1))}
          spellCheck={false}
          className="w-full rounded-md border border-input bg-input p-2 font-mono text-[12px] text-foreground"
        />
        <div className="flex flex-wrap items-center gap-2">
          <button
            type="button"
            disabled={busy}
            onClick={() => void run(() => saveAgentInterface(agentSlug, draft))}
            className="rounded-md bg-primary px-3 py-1 text-[12px] font-medium text-primary-foreground disabled:opacity-50"
          >
            {busy ? 'Saving…' : 'Save'}
          </button>
          <button type="button" onClick={() => setDraft(null)} className="text-[12px] text-muted-foreground underline">
            Cancel
          </button>
        </div>
        {error && <p role="alert" className="m-0 whitespace-pre-wrap text-[12px] text-destructive">{error}</p>}
      </div>
    )
  }

  const iface = (data.interface ?? {}) as { full?: string[]; capabilities?: Record<string, Capability> }
  const caps = iface.capabilities ?? {}
  const names = Object.keys(caps)
  const full = iface.full ?? []
  const published = names.length > 0 || full.length > 0

  return (
    <div className="flex flex-col gap-2">
      {!published && (
        <p data-testid="interface-none" className="m-0 text-[13px] text-foreground-secondary">
          Not published. Everyone who can reach this agent gets all of it.
        </p>
      )}
      {full.length > 0 && (
        <div data-testid="interface-full" className="text-[13px] text-foreground">
          <span className="text-[12px] text-muted-foreground">The whole agent, as its admins have it: </span>
          <span className="font-mono text-[12px]">{full.join(', ')}</span>
        </div>
      )}
      {names.length > 0 && (
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
      )}
      {data.published_at && (
        <p className="m-0 text-[11px] text-muted-foreground">
          Saved {new Date(data.published_at).toLocaleString()}
          {data.published_by_email ? ` by ${data.published_by_email}` : ''}. Everything not listed is refused.
        </p>
      )}
      {canEdit && (
        <div className="flex flex-wrap items-center gap-3">
          <button
            type="button"
            onClick={startEdit}
            className="rounded-md border border-border bg-input px-3 py-1 text-[12px] font-medium text-foreground hover:border-primary"
          >
            {published ? 'Edit' : 'Set up'}
          </button>
          {published && (
            <button
              type="button"
              disabled={busy}
              onClick={() => void run(() => unpublishAgentInterface(agentSlug))}
              className="text-[12px] text-muted-foreground underline hover:text-destructive disabled:opacity-50"
            >
              Unpublish
            </button>
          )}
        </div>
      )}
      {error && <p role="alert" className="m-0 text-[12px] text-destructive">{error}</p>}
    </div>
  )
}
