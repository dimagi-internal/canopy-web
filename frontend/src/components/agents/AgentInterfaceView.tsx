import { useEffect, useState } from 'react'
import {
  getAgentInterface,
  getAgentInterfaceHistory,
  resetAgentInterface,
  setCapabilitySensitive,
  type AgentInterfaceOut,
  type SettingChangeOut,
} from '@/api/agents'
import { InterfaceEditor } from './InterfaceEditor'
import type { Capability } from './interfaceModel'

// The capabilities an interface marks as handling sensitive data.
function sensitiveCapabilities(iface: unknown): string[] {
  const caps = ((iface ?? {}) as { capabilities?: Record<string, Capability> }).capabilities ?? {}
  return Object.keys(caps).filter((n) => caps[n]?.data === 'sensitive')
}

// What one audited change did to sensitive data: on when the interface it saved marks a capability
// sensitive, off when it did not or it reset to the default.
function sensitiveAfter(c: SettingChangeOut): boolean {
  if (c.operation === 'unpublish_interface' || c.operation === 'reset_interface') return false
  if (c.operation === 'set_capability_sensitive') {
    const after = (c.changes?.[0]?.after ?? {}) as { sensitive_capabilities?: string[] }
    return (after.sensitive_capabilities ?? []).length > 0
  }
  const body = (c.request?.body ?? {}) as { source?: string; interface?: unknown }
  return body.source != null
    ? /(^|[\s{,])["']?data["']?\s*:\s*["']?sensitive\b/m.test(body.source.replace(/(^|\s)#.*$/gm, ''))
    : sensitiveCapabilities(body.interface).length > 0
}

// What one audited change was, in a word or three.
function changeLabel(c: SettingChangeOut): string {
  if (c.operation === 'unpublish_interface') return 'Unpublished'
  if (c.operation === 'reset_interface') return 'Reset to default'
  if (c.operation === 'set_capability_sensitive') {
    const on = Boolean((c.request?.body as { handles_sensitive?: boolean } | undefined)?.handles_sensitive)
    return `Sensitive data ${on ? 'on' : 'off'} for ${c.target.split(' / ')[1] ?? 'a capability'}`
  }
  return 'Saved'
}

// The agent's declared interface: the capabilities it offers viewers and users, each with the
// lowest role that may use it. Who has which role is the Access setting, not this. LIVE STATE held
// by canopy-web — not a file in the agent's repo — so agent admins edit it here (a form, with the
// YAML one click away), and anyone who can see the agent can read it.
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
  const [editing, setEditing] = useState(false)
  const [busy, setBusy] = useState(false)
  const [history, setHistory] = useState<SettingChangeOut[] | null>(null)

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
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Could not save')
    } finally {
      setBusy(false)
    }
  }

  if (data === null) {
    return error
      ? <p role="alert" className="m-0 text-[12px] text-destructive">{error}</p>
      : <p className="m-0 text-[12px] text-muted-foreground">Loading…</p>
  }

  if (editing) {
    return (
      <InterfaceEditor
        agentSlug={agentSlug}
        saved={data}
        onSaved={(out) => {
          setData(out)
          setEditing(false)
        }}
        onCancel={() => setEditing(false)}
      />
    )
  }

  const iface = (data.interface ?? {}) as { capabilities?: Record<string, Capability> }
  const caps = iface.capabilities ?? {}
  const names = Object.keys(caps)
  const isDefault = data.is_default

  return (
    <div className="flex flex-col gap-2">
      {isDefault && (
        <p data-testid="interface-default" className="m-0 text-[13px] text-foreground">
          {agentName ?? agentSlug} offers no capabilities (default), so viewers and users cannot use it. Who may use
          it is set on the Access setting.
        </p>
      )}
      {names.length > 0 && (
        <p className="m-0 text-[12px] text-muted-foreground">
          Each capability names the lowest role that may use it. Who has which role is set on the Access setting.
        </p>
      )}
      {names.length > 0 && (
        <table className="w-full text-[13px]">
          <thead>
            <tr className="text-left text-[11px] text-muted-foreground">
              <th className="py-1 font-medium">Capability</th>
              <th className="py-1 font-medium">Lowest role</th>
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
                    {c.role === 'viewer' ? 'Viewer' : 'User'}
                  </td>
                  <td className="py-1.5 align-top font-mono text-[11px] text-foreground-secondary">
                    {[...(c.sites ?? []).map((s) => `site: ${s}`), ...(c.tools ?? []), ...(c.bash ?? [])].map((t) => (
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
      {!isDefault && data.published_at && (
        <p className="m-0 text-[11px] text-muted-foreground">
          Saved {new Date(data.published_at).toLocaleString()}
          {data.published_by_email ? ` by ${data.published_by_email}` : ''}. Everything not listed is refused.
        </p>
      )}
      {canEdit && (
        <div className="flex flex-wrap items-center gap-3">
          <button
            type="button"
            onClick={() => setEditing(true)}
            className="rounded-md border border-border bg-input px-3 py-1 text-[12px] font-medium text-foreground hover:border-primary"
          >
            {isDefault ? 'Add capabilities' : 'Edit'}
          </button>
          {!isDefault && (
            <button
              type="button"
              disabled={busy}
              onClick={() => void run(() => resetAgentInterface(agentSlug))}
              className="text-[12px] text-muted-foreground underline hover:text-destructive disabled:opacity-50"
            >
              Reset to default
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
