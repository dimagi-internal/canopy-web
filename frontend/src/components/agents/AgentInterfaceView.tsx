import { useEffect, useState } from 'react'
import {
  getAgentInterface,
  getAgentInterfaceHistory,
  resetAgentInterface,
  saveAgentInterfaceModel,
  setCapabilitySensitive,
  type AgentInterfaceOut,
  type SettingChangeOut,
} from '@/api/agents'
import { InterfaceEditor } from './InterfaceEditor'
import {
  clone,
  isReadMode,
  overrideFromDefault,
  withCapability,
  withoutCapability,
  type Capability,
  type InterfaceModel,
} from './interfaceModel'
import { Td, Th, Tr } from '@/components/DataTable'

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

type Profile = {
  entry?: string | null
  tools?: string[]
  bash?: string[]
  read_paths?: string[]
  write_paths?: string[]
}

// canopy's default read profile, in plain words (the pinned list lives in
// tests/test_default_read_profile.py; this only says it).
export function describeProfile(p: Profile): string[] {
  const out: string[] = []
  out.push(`Can read: ${(p.read_paths ?? []).join(', ') || 'nothing'}`)
  const tools = (p.tools ?? []).filter((t) => !t.includes('who_is_asking'))
  out.push(`Tools: ${tools.join(', ') || 'none'}, and who_is_asking (to see who is asking)`)
  const writes = p.write_paths ?? []
  out.push(writes.length ? `Can write only to: ${writes.join(', ')} (it drafts its reply there)` : 'Can write nothing')
  const bash = p.bash ?? []
  out.push(bash.length ? `Can run only these commands: ${bash.join(' | ')}` : 'Can run no commands')
  out.push(p.entry ? `Starts with: ${p.entry}` : "Starts with the caller's message and no command (it answers from its persona and CLAUDE.md)")
  return out
}

// The agent's read mode and doors. Read mode is how it answers someone who may read but not drive
// it; it exists on every agent, and an agent may override it whole with its own `ask`. A door is a
// further confined profile for a place or a list of people, and rare. Who may open which is a
// line on the Access setting, not this. LIVE STATE held
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
  // The draft the editor starts from when overriding read mode (a copy of the default profile).
  const [seed, setSeed] = useState<InterfaceModel | undefined>(undefined)
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
        seed={seed}
        onSaved={(out) => {
          setData(out)
          setEditing(false)
          setSeed(undefined)
        }}
        onCancel={() => {
          setEditing(false)
          setSeed(undefined)
        }}
      />
    )
  }

  const iface = (data.interface ?? {}) as { capabilities?: Record<string, Capability> }
  const caps = iface.capabilities ?? {}
  const names = Object.keys(caps)
  const isDefault = data.is_default
  const readMode = data.read_mode
  const overriding = readMode.source === 'override'
  const name = agentName ?? agentSlug
  const startOverride = () => {
    setSeed(withCapability(clone(data.interface as InterfaceModel), 'ask', overrideFromDefault(readMode.default_profile)))
    setEditing(true)
  }

  return (
    <div className="flex flex-col gap-2">
      <div data-testid="read-mode" className="flex flex-col gap-1 rounded-md border border-border p-2">
        <div className="text-[12px] font-medium text-foreground">Read mode</div>
        {overriding ? (
          <p data-testid="read-mode-override" className="m-0 text-[12px] text-foreground">
            {name} has its own <span className="font-mono">{readMode.override}</span> capability, which replaces canopy&apos;s default
            read mode whole. Edit it below.
          </p>
        ) : (
          <>
            <p data-testid="read-mode-default" className="m-0 text-[12px] text-foreground">
              People who may read {name} but not drive it are answered in read mode: a confined session that can only read and
              draft a reply, on every agent from the start. This is canopy&apos;s default for {name} (version {readMode.default_profile_version}):
            </p>
            <ul data-testid="read-mode-profile" className="m-0 list-disc pl-5 text-[12px] text-foreground-secondary">
              {describeProfile(readMode.default_profile).map((l) => <li key={l} className="break-words">{l}</li>)}
            </ul>
          </>
        )}
        {canEdit && !overriding && (
          <div>
            <button
              type="button"
              onClick={startOverride}
              className="rounded-md border border-border bg-input px-3 py-1 text-[12px] font-medium text-foreground hover:border-primary"
            >
              Override
            </button>
            <span className="ml-2 text-[11px] text-muted-foreground">starts from a copy of the default; it replaces it whole</span>
          </div>
        )}
        {canEdit && overriding && (
          <div>
            <button
              type="button"
              disabled={busy}
              onClick={() => void run(() => saveAgentInterfaceModel(agentSlug, withoutCapability(data.interface as InterfaceModel, readMode.override ?? 'ask')))}
              className="text-[12px] text-muted-foreground underline hover:text-destructive disabled:opacity-50"
            >
              Go back to the default read mode
            </button>
          </div>
        )}
      </div>
      {isDefault && (
        <p data-testid="interface-default" className="m-0 text-[13px] text-foreground">
          {name} has no doors (default), only read mode. Who may use it is set on the Access setting.
        </p>
      )}
      {names.length > 0 && (
        <p className="m-0 text-[12px] text-muted-foreground">
          Who may open a door is set on the Access setting: read only gets read mode, a line can name any door.
        </p>
      )}
      {names.length > 0 && (
        <table className="w-full text-[13px]">
          <thead>
            <Tr head="small">
              <Th>Door</Th>
              <Th>Allows</Th>
              <Th>Sensitive data</Th>
            </Tr>
          </thead>
          <tbody className="divide-y divide-border">
            {names.map((n) => {
              const c = caps[n]
              return (
                <tr key={n} data-testid="interface-capability">
                  <Td density="cozy" tone="foreground" className="align-top">
                    <span className="font-mono">{n}</span>
                    {isReadMode(n) && <span className="ml-2 rounded bg-muted px-1.5 py-0.5 text-[11px] text-muted-foreground">read mode override</span>}
                    {c.description && <div className="text-[12px] text-muted-foreground">{c.description}</div>}
                  </Td>
                  <Td density="cozy" tone="secondary" mono className="align-top text-[11px]">
                    {[...(c.sites ?? []).map((s) => `site: ${s}`), ...(c.tools ?? []), ...(c.bash ?? [])].map((t) => (
                      <div key={t}>{t}</div>
                    ))}
                  </Td>
                  <Td density="cozy" className="align-top text-[12px]">
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
                  </Td>
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
            {isDefault ? 'Add a door' : 'Edit'}
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
              <Tr head="small">
                <Th>When</Th>
                <Th>Who</Th>
                <Th>Through</Th>
                <Th>Change</Th>
                <Th>Sensitive data after</Th>
              </Tr>
            </thead>
            <tbody className="divide-y divide-border">
              {history.slice(0, 10).map((c) => (
                <tr key={c.id} data-testid="interface-change">
                  <Td tone="secondary">{new Date(c.created_at).toLocaleString()}</Td>
                  <Td tone="secondary">{c.actor_email || 'someone'}</Td>
                  <Td tone="secondary">{c.via}</Td>
                  <Td tone="secondary">{changeLabel(c)}</Td>
                  <Td>{sensitiveAfter(c) ? 'on' : 'off'}</Td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
