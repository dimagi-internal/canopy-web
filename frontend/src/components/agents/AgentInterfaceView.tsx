import { useEffect, useState } from 'react'
import {
  getAgentInterface,
  getAgentInterfaceHistory,
  saveAgentInterface,
  unpublishAgentInterface,
  type AgentInterfaceOut,
  type SettingChangeOut,
} from '@/api/agents'

type Capability = {
  description?: string
  callers?: string[]
  entry?: string | null
  tools?: string[]
  bash?: string[]
  input?: Record<string, string>
  data?: string
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

// A `data: phi` line, the block form an author writes; JSON (`"data": "phi"`)
// is caught by PHI_ANYWHERE but never edited.
const PHI_LINE = /^\s*data\s*:\s*["']?phi["']?\s*(#.*)?$/
const PHI_ANYWHERE = /(^|[\s{,])["']?data["']?\s*:\s*["']?phi\b/m

// Does this YAML mark any capability PHI-safe? Comments do not count. The
// server parses it for real; this only decides what the editor says.
export function declaresPhi(source: string): boolean {
  return PHI_ANYWHERE.test(source.split('\n').map((l) => l.replace(/(^|\s)#.*$/, '')).join('\n'))
}

// The same YAML with every `data: phi` line taken out: saving it turns PHI-safe off.
function withoutPhi(source: string): string {
  return source.split('\n').filter((l) => !PHI_LINE.test(l)).join('\n')
}

// The capabilities an interface marks PHI-safe.
function phiCapabilities(iface: unknown): string[] {
  const caps = ((iface ?? {}) as { capabilities?: Record<string, Capability> }).capabilities ?? {}
  return Object.keys(caps).filter((n) => caps[n]?.data === 'phi')
}

// What one audited change did to PHI-safe: on when the YAML it saved says
// `data: phi`, off when it did not or it unpublished.
function phiAfter(c: SettingChangeOut): boolean {
  if (c.operation === 'unpublish_interface') return false
  const body = (c.request?.body ?? {}) as { source?: string; interface?: unknown }
  return body.source != null ? declaresPhi(body.source) : phiCapabilities(body.interface).length > 0
}

// The agent's declared interface: who may make it do what. LIVE STATE held by
// canopy-web — not a file in the agent's repo — so owners and admins edit it
// here, and every member can read it.
export function AgentInterfaceView({
  agentSlug,
  agentName,
  canEdit = false,
}: {
  agentSlug: string
  agentName?: string
  canEdit?: boolean
}) {
  const [data, setData] = useState<AgentInterfaceOut | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [draft, setDraft] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [history, setHistory] = useState<SettingChangeOut[] | null>(null)

  const phiNow = phiCapabilities(data?.interface)
  const draftPhi = draft !== null && declaresPhi(draft)

  useEffect(() => {
    let cancelled = false
    getAgentInterface(agentSlug)
      .then((d) => !cancelled && setData(d))
      .catch((e: unknown) => !cancelled && setError(e instanceof Error ? e.message : 'Could not load'))
    return () => {
      cancelled = true
    }
  }, [agentSlug])

  // The audit trail, re-read after every save. Best effort: without it the
  // interface still shows.
  useEffect(() => {
    let cancelled = false
    getAgentInterfaceHistory(agentSlug)
      .then((h) => !cancelled && setHistory(h))
      .catch(() => !cancelled && setHistory([]))
    return () => {
      cancelled = true
    }
  }, [agentSlug, data])

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

  const startEdit = (opts: { phiOff?: boolean } = {}) => {
    const iface = data?.interface ?? {}
    // JSON is valid YAML, so an interface saved as a mapping still round-trips.
    const source = data?.source || (Object.keys(iface).length ? JSON.stringify(iface, null, 2) : STARTER)
    setDraft(opts.phiOff ? withoutPhi(source) : source)
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
        {draftPhi && (
          <p data-testid="phi-on" className="m-0 rounded-md border border-warning/30 bg-warning/10 px-2 py-1 text-[12px] text-foreground">
            Every capability with <code>data: phi</code> is PHI-safe: you vouch that it reads a host&apos;s data only
            through canopy&apos;s gateway and keeps none of it. Saving asks for your passkey and is logged with the
            agent&apos;s other setting changes.
          </p>
        )}
        {!draftPhi && phiNow.length > 0 && (
          <p data-testid="phi-off" className="m-0 rounded-md border border-warning/30 bg-warning/10 px-2 py-1 text-[12px] text-warning">
            No capability says <code>data: phi</code> any more: saving turns PHI-safe off for {phiNow.join(', ')},
            and hosts stop giving it their data.
          </p>
        )}
        <div className="flex flex-wrap items-center gap-2">
          <button
            type="button"
            disabled={busy}
            onClick={() => void run(() => saveAgentInterface(agentSlug, draft))}
            className="rounded-md bg-primary px-3 py-1 text-[12px] font-medium text-primary-foreground disabled:opacity-50"
          >
            {busy
              ? 'Saving…'
              : draftPhi && phiNow.length === 0
                ? 'Save and turn on PHI-safe'
                : !draftPhi && phiNow.length > 0
                  ? 'Save and turn off PHI-safe'
                  : 'Save'}
          </button>
          <button type="button" onClick={() => setDraft(null)} className="text-[12px] text-muted-foreground underline">
            Cancel
          </button>
          {draftPhi && <span className="text-[11px] text-muted-foreground">Asks for your passkey.</span>}
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
          No outside callers. Only workspace members can use {agentName ?? agentSlug}.
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
              <th className="py-1 font-medium">PHI</th>
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
                    {Object.keys(c.input ?? {}).length > 0 && (
                      <div className="text-[11px] text-muted-foreground">
                        Inputs: {Object.entries(c.input ?? {}).map(([k, t]) => `${k}: ${t}`).join(', ')}
                      </div>
                    )}
                  </td>
                  <td className="py-1.5 align-top text-[12px] text-foreground-secondary">
                    {(c.callers ?? []).join(', ') || 'nobody'}
                  </td>
                  <td className="py-1.5 align-top font-mono text-[11px] text-foreground-secondary">
                    {[...(c.tools ?? []), ...(c.bash ?? [])].map((t) => (
                      <div key={t}>{t}</div>
                    ))}
                  </td>
                  <td className="py-1.5 align-top text-[12px]">
                    {c.data === 'phi' ? (
                      <span data-testid="phi-safe" className="rounded bg-warning/10 px-1.5 py-0.5 font-medium text-warning">
                        PHI-safe
                      </span>
                    ) : (
                      <span className="text-muted-foreground">—</span>
                    )}
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
      {(data.warnings ?? []).map((w) => (
        <p
          key={w}
          data-testid="interface-warning"
          className="m-0 rounded-md border border-warning/30 bg-warning/10 px-2 py-1 text-[12px] text-warning"
        >
          {w}
        </p>
      ))}
      {canEdit && (
        <div className="flex flex-wrap items-center gap-3">
          <button
            type="button"
            onClick={() => startEdit()}
            className="rounded-md border border-border bg-input px-3 py-1 text-[12px] font-medium text-foreground hover:border-primary"
          >
            {published ? 'Edit' : 'Set up'}
          </button>
          {phiNow.length > 0 && (
            <button
              type="button"
              onClick={() => startEdit({ phiOff: true })}
              className="rounded-md border border-border bg-input px-3 py-1 text-[12px] font-medium text-foreground hover:border-destructive"
            >
              Turn off PHI-safe
            </button>
          )}
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
      {history && history.length > 0 && (
        <div data-testid="interface-history" className="flex flex-col gap-1">
          <div className="text-[12px] font-medium text-foreground">Change history</div>
          <table className="w-full text-[12px]">
            <thead>
              <tr className="text-left text-[11px] text-muted-foreground">
                <th className="py-1 font-medium">When</th>
                <th className="py-1 font-medium">Who</th>
                <th className="py-1 font-medium">Through</th>
                <th className="py-1 font-medium">Change</th>
                <th className="py-1 font-medium">PHI-safe after</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {history.slice(0, 10).map((c) => (
                <tr key={c.id} data-testid="interface-change">
                  <td className="py-1 text-foreground-secondary">{new Date(c.created_at).toLocaleString()}</td>
                  <td className="py-1 text-foreground-secondary">{c.actor_email || 'someone'}</td>
                  <td className="py-1 text-foreground-secondary">{c.via}</td>
                  <td className="py-1 text-foreground-secondary">
                    {c.operation === 'unpublish_interface' ? 'Unpublished' : 'Saved'}
                  </td>
                  <td className="py-1">{phiAfter(c) ? 'on' : 'off'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
