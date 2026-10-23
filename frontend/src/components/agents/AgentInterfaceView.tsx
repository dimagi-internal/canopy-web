import { useEffect, useState } from 'react'
import {
  getAgentInterface,
  getAgentInterfaceHistory,
  saveAgentInterface,
  setCapabilitySensitive,
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

// A `data: sensitive` line, the block form an author writes; JSON (`"data": "sensitive"`)
// is caught by SENSITIVE_ANYWHERE but never edited.
const SENSITIVE_LINE = /^\s*data\s*:\s*["']?sensitive["']?\s*(#.*)?$/
const SENSITIVE_ANYWHERE = /(^|[\s{,])["']?data["']?\s*:\s*["']?sensitive\b/m

// Does this YAML mark any capability as handling sensitive data? Comments do not
// count. The server parses it for real; this only decides what the editor says.
export function declaresSensitive(source: string): boolean {
  return SENSITIVE_ANYWHERE.test(source.split('\n').map((l) => l.replace(/(^|\s)#.*$/, '')).join('\n'))
}

// The same YAML with every `data: sensitive` line taken out: saving it turns
// sensitive data off.
function withoutSensitive(source: string): string {
  return source.split('\n').filter((l) => !SENSITIVE_LINE.test(l)).join('\n')
}

// The capabilities an interface marks as handling sensitive data.
function sensitiveCapabilities(iface: unknown): string[] {
  const caps = ((iface ?? {}) as { capabilities?: Record<string, Capability> }).capabilities ?? {}
  return Object.keys(caps).filter((n) => caps[n]?.data === 'sensitive')
}

// What one audited change did to sensitive data: on when the YAML it saved says
// `data: sensitive`, off when it did not or it unpublished.
function sensitiveAfter(c: SettingChangeOut): boolean {
  if (c.operation === 'unpublish_interface') return false
  if (c.operation === 'set_capability_sensitive') {
    const after = (c.changes?.[0]?.after ?? {}) as { sensitive_capabilities?: string[] }
    return (after.sensitive_capabilities ?? []).length > 0
  }
  const body = (c.request?.body ?? {}) as { source?: string; interface?: unknown }
  return body.source != null ? declaresSensitive(body.source) : sensitiveCapabilities(body.interface).length > 0
}

// What one audited change was, in a word or three.
function changeLabel(c: SettingChangeOut): string {
  if (c.operation === 'unpublish_interface') return 'Unpublished'
  if (c.operation === 'set_capability_sensitive') {
    const on = Boolean((c.request?.body as { handles_sensitive?: boolean } | undefined)?.handles_sensitive)
    return `Sensitive data ${on ? 'on' : 'off'} for ${c.target.split(' / ')[1] ?? 'a capability'}`
  }
  return 'Saved'
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

  const sensitiveNow = sensitiveCapabilities(data?.interface)
  const draftSensitive = draft !== null && declaresSensitive(draft)

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

  const startEdit = (opts: { sensitiveOff?: boolean } = {}) => {
    const iface = data?.interface ?? {}
    // JSON is valid YAML, so an interface saved as a mapping still round-trips.
    const source = data?.source || (Object.keys(iface).length ? JSON.stringify(iface, null, 2) : STARTER)
    setDraft(opts.sensitiveOff ? withoutSensitive(source) : source)
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
        {draftSensitive && (
          <p data-testid="sensitive-on" className="m-0 rounded-md border border-warning/30 bg-warning/10 px-2 py-1 text-[12px] text-foreground">
            Every capability with <code>data: sensitive</code> handles sensitive data: you vouch that it reads a
            host&apos;s data only through canopy&apos;s gateway and keeps none of it. Saving is logged with the agent&apos;s other setting
            changes.
          </p>
        )}
        {!draftSensitive && sensitiveNow.length > 0 && (
          <p data-testid="sensitive-off" className="m-0 rounded-md border border-warning/30 bg-warning/10 px-2 py-1 text-[12px] text-warning">
            No capability says <code>data: sensitive</code> any more: saving turns sensitive data off for{' '}
            {sensitiveNow.join(', ')},
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
              : draftSensitive && sensitiveNow.length === 0
                ? 'Save and turn on sensitive data'
                : !draftSensitive && sensitiveNow.length > 0
                  ? 'Save and turn off sensitive data'
                  : 'Save'}
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
              <th className="py-1 font-medium">Sensitive data</th>
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
                    <div className="flex flex-wrap items-center gap-2">
                      {c.data === 'sensitive' ? (
                        <span data-testid="handles-sensitive" className="rounded bg-warning/10 px-1.5 py-0.5 font-medium text-warning">
                          Handles sensitive data
                        </span>
                      ) : (
                        <span className="text-muted-foreground">—</span>
                      )}
                      {canEdit && (
                        <button
                          type="button"
                          role="switch"
                          aria-checked={c.data === 'sensitive'}
                          aria-label={`Handles sensitive data for ${n}`}
                          data-testid="sensitive-switch"
                          disabled={busy}
                          onClick={() => void run(() => setCapabilitySensitive(agentSlug, n, c.data !== 'sensitive'))}
                          className="text-[11px] text-muted-foreground underline hover:text-foreground disabled:opacity-50"
                        >
                          {c.data === 'sensitive' ? 'Turn off' : 'Turn on'}
                        </button>
                      )}
                    </div>
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
          {sensitiveNow.length > 0 && (
            <button
              type="button"
              onClick={() => startEdit({ sensitiveOff: true })}
              className="rounded-md border border-border bg-input px-3 py-1 text-[12px] font-medium text-foreground hover:border-destructive"
            >
              Turn off sensitive data
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
                <th className="py-1 font-medium">Sensitive data after</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {history.slice(0, 10).map((c) => (
                <tr key={c.id} data-testid="interface-change">
                  <td className="py-1 text-foreground-secondary">{new Date(c.created_at).toLocaleString()}</td>
                  <td className="py-1 text-foreground-secondary">{c.actor_email || 'someone'}</td>
                  <td className="py-1 text-foreground-secondary">{c.via}</td>
                  <td className="py-1 text-foreground-secondary">{changeLabel(c)}</td>
                  <td className="py-1">{sensitiveAfter(c) ? 'on' : 'off'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
