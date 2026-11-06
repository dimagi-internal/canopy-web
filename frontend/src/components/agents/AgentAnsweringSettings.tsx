import { useEffect, useState } from 'react'
import {
  deleteAgentHostSite,
  getAgentSettingsHistory,
  getReadMode,
  listAgentHostSites,
  putAgentHostSite,
  putReadMode,
  resetReadMode,
  type AnsweringProfile,
  type AgentHostSiteOut,
  type AgentHostSitesOut,
  type ReadModeSettingOut,
  type SettingChangeOut,
} from '@/api/agents'
import { Td, Th, Tr } from '@/components/DataTable'

// How an agent answers people who may read but not drive it, and the sites it can be reached from.
// Live state on canopy-web: agent admins edit it here, and anyone who can see the agent can read it.
// Who may use each is the access list on the Roles tab, not this.

const FIELD = 'rounded-md border border-border bg-input px-2 py-1 text-[12px] text-foreground'
const BUTTON = 'rounded-md border border-border bg-input px-3 py-1 text-[12px] font-medium text-foreground hover:border-primary disabled:opacity-50'
const QUIET = 'text-[12px] text-muted-foreground underline hover:text-foreground disabled:opacity-50'
const DANGER = 'text-[12px] text-muted-foreground underline hover:text-destructive disabled:opacity-50'
/** The canopy app's own widget is registered as a site under this name. */
const THIS_APP = 'canopy-web'

function siteName(site: string): string {
  return site === THIS_APP ? 'canopy (this app)' : site
}

const lines = (text: string) => text.split('\n').map((l) => l.trim()).filter(Boolean)

/** A profile in plain words. */
export function describeProfile(p: Partial<AnsweringProfile>): string[] {
  const out: string[] = []
  out.push(`Can read: ${(p.read_paths ?? []).join(', ') || 'nothing'}`)
  const tools = (p.tools ?? []).filter((t) => !t.includes('who_is_asking'))
  out.push(`Tools: ${tools.join(', ') || 'none'}, and who_is_asking (to see who is asking)`)
  const writes = p.write_paths ?? []
  out.push(writes.length ? `Can write only to: ${writes.join(', ')} (it drafts its reply there)` : 'Can write nothing')
  const bash = p.bash ?? []
  out.push(bash.length ? `Can run only these commands: ${bash.join(' | ')}` : 'Can run no commands')
  out.push(p.entry ? `Starts with: ${p.entry}` : "Starts with the caller's message and no command")
  return out
}

type Draft = Record<'description' | 'entry' | 'tools' | 'bash' | 'read_paths' | 'write_paths', string>

function toDraft(p: AnsweringProfile): Draft {
  return {
    description: p.description ?? '',
    entry: p.entry ?? '',
    tools: (p.tools ?? []).join('\n'),
    bash: (p.bash ?? []).join('\n'),
    read_paths: (p.read_paths ?? []).join('\n'),
    write_paths: (p.write_paths ?? []).join('\n'),
  }
}

function fromDraft(d: Draft): AnsweringProfile {
  return {
    description: d.description.trim(),
    entry: d.entry.trim() || null,
    tools: lines(d.tools),
    bash: lines(d.bash),
    read_paths: lines(d.read_paths),
    write_paths: lines(d.write_paths),
  }
}

function TextField({ label, value, onChange }: { label: string; value: string; onChange: (v: string) => void }) {
  return (
    <label className="flex flex-col gap-0.5 text-[12px] text-foreground">
      <span>{label}</span>
      <input aria-label={label} value={value} onChange={(e) => onChange(e.target.value)} className={FIELD} />
    </label>
  )
}

function ListField({ label, hint, value, onChange }: { label: string; hint: string; value: string; onChange: (v: string) => void }) {
  return (
    <label className="flex flex-col gap-0.5 text-[12px] text-foreground">
      <span>{label} <span className="text-muted-foreground">{hint}</span></span>
      <textarea
        aria-label={label}
        rows={Math.min(8, Math.max(2, value.split('\n').length))}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className={`${FIELD} font-mono text-[11px]`}
      />
    </label>
  )
}

function ProfileFields({ draft, set }: { draft: Draft; set: (patch: Partial<Draft>) => void }) {
  return (
    <>
      <TextField label="What it says it is for" value={draft.description} onChange={(description) => set({ description })} />
      <TextField label="Starts with" value={draft.entry} onChange={(entry) => set({ entry })} />
      <ListField label="Tools it may use" hint="one per line" value={draft.tools} onChange={(tools) => set({ tools })} />
      <ListField label="Commands it may run" hint="one per line; none is safest" value={draft.bash} onChange={(bash) => set({ bash })} />
      <ListField label="Folders it may read" hint="one per line" value={draft.read_paths} onChange={(read_paths) => set({ read_paths })} />
      <ListField label="Folders it may write" hint="one per line; none means it writes nothing" value={draft.write_paths} onChange={(write_paths) => set({ write_paths })} />
    </>
  )
}

function Failure({ error }: { error: string | null }) {
  return error ? <p role="alert" className="m-0 text-[12px] text-destructive">{error}</p> : null
}

function useSave<T>(apply: (out: T) => void) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const run = async (fn: () => Promise<T>): Promise<boolean> => {
    setBusy(true)
    setError(null)
    try {
      apply(await fn())
      return true
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Could not save')
      return false
    } finally {
      setBusy(false)
    }
  }
  return { busy, error, setError, run }
}

export function ReadModeCard({ agentSlug, agentName, canEdit, onChange }: {
  agentSlug: string; agentName: string; canEdit: boolean; onChange: () => void
}) {
  const [data, setData] = useState<ReadModeSettingOut | null>(null)
  const [draft, setDraft] = useState<Draft | null>(null)
  const { busy, error, setError, run } = useSave<ReadModeSettingOut>((out) => { setData(out); onChange() })

  useEffect(() => {
    let cancelled = false
    getReadMode(agentSlug)
      .then((d) => !cancelled && setData(d))
      .catch((e: unknown) => !cancelled && setError(e instanceof Error ? e.message : 'Could not load'))
    return () => { cancelled = true }
  }, [agentSlug, setError])

  if (data === null) return error ? <Failure error={error} /> : <p className="m-0 text-[12px] text-muted-foreground">Loading…</p>
  const custom = data.source === 'custom'
  return (
    <div data-testid="read-mode" className="flex flex-col gap-2 rounded-md border border-border p-2">
      <div className="text-[12px] font-medium text-foreground">Read mode</div>
      <p className="m-0 text-[12px] text-foreground">
        People who may read {agentName} but not drive it are answered in a confined session.{' '}
        {custom ? `${agentName} uses its own list.` : `${agentName} is on canopy's default.`}
      </p>
      {draft === null ? (
        <ul data-testid="read-mode-profile" className="m-0 list-disc pl-5 text-[12px] text-foreground-secondary">
          {describeProfile(data.profile).map((l) => <li key={l} className="break-words">{l}</li>)}
        </ul>
      ) : (
        <form
          className="flex flex-col gap-2"
          onSubmit={(e) => {
            e.preventDefault()
            void run(() => putReadMode(agentSlug, fromDraft(draft))).then((saved) => saved && setDraft(null))
          }}
        >
          <ProfileFields draft={draft} set={(patch) => setDraft({ ...draft, ...patch })} />
          <div className="flex items-center gap-3">
            <button type="submit" disabled={busy} className={BUTTON}>Save</button>
            <button type="button" onClick={() => setDraft(null)} className={QUIET}>Cancel</button>
          </div>
        </form>
      )}
      {canEdit && draft === null && (
        <div className="flex items-center gap-3">
          <button type="button" onClick={() => setDraft(toDraft(data.profile))} className={BUTTON}>
            {custom ? 'Change' : 'Customise'}
          </button>
          {custom && (
            <button type="button" disabled={busy} className={DANGER} onClick={() => void run(() => resetReadMode(agentSlug))}>
              Go back to the default
            </button>
          )}
        </div>
      )}
      <Failure error={error} />
    </div>
  )
}

type SiteDraft = Draft & { ceiling: string; own_login: string }

function SiteForm({ row, busy, onSave, onCancel }: {
  row: AgentHostSiteOut; busy: boolean; onSave: (fields: object) => void; onCancel: () => void
}) {
  const [draft, setDraft] = useState<SiteDraft>({
    ...toDraft(row), ceiling: (row.ceiling ?? []).join('\n'), own_login: (row.own_login ?? []).join('\n'),
  })
  const set = (patch: Partial<SiteDraft>) => setDraft({ ...draft, ...patch })
  return (
    <form
      className="flex flex-col gap-2 py-1"
      onSubmit={(e) => {
        e.preventDefault()
        onSave({ ...fromDraft(draft), ceiling: lines(draft.ceiling), own_login: lines(draft.own_login) })
      }}
    >
      <ListField label="Site tools it may use" hint="one per line; none means whatever the site allows" value={draft.ceiling} onChange={(ceiling) => set({ ceiling })} />
      <ListField label="Its own login on this site" hint="credential names, one per line; kept out of its conversations here" value={draft.own_login} onChange={(own_login) => set({ own_login })} />
      <ProfileFields draft={draft} set={set} />
      <div className="flex items-center gap-3">
        <button type="submit" disabled={busy} className={BUTTON}>Save</button>
        <button type="button" onClick={onCancel} className={QUIET}>Cancel</button>
      </div>
    </form>
  )
}

export function HostSitesCard({ agentSlug, agentName, canEdit, onChange }: {
  agentSlug: string; agentName: string; canEdit: boolean; onChange: () => void
}) {
  const [data, setData] = useState<AgentHostSitesOut | null>(null)
  const [editing, setEditing] = useState<string | null>(null)
  const [adding, setAdding] = useState('')
  const { busy, error, setError, run } = useSave<AgentHostSitesOut>((out) => { setData(out); onChange() })

  useEffect(() => {
    let cancelled = false
    listAgentHostSites(agentSlug)
      .then((d) => !cancelled && setData(d))
      .catch((e: unknown) => !cancelled && setError(e instanceof Error ? e.message : 'Could not load'))
    return () => { cancelled = true }
  }, [agentSlug, setError])

  if (data === null) return error ? <Failure error={error} /> : <p className="m-0 text-[12px] text-muted-foreground">Loading…</p>
  const connected = new Set(data.sites.map((r) => r.site))
  const addable = data.available.filter((s) => !connected.has(s))
  return (
    <div data-testid="host-sites" className="flex flex-col gap-2 rounded-md border border-border p-2">
      <div className="text-[12px] font-medium text-foreground">Host sites</div>
      {data.sites.length === 0 ? (
        <p className="m-0 text-[12px] text-muted-foreground">{agentName} cannot be reached from any site.</p>
      ) : (
        <table className="w-full text-[12px]">
          <thead>
            <Tr head="small">
              <Th>Site</Th>
              <Th>Site tools it may use</Th>
              <Th>Its own login</Th>
              <Th>Handles sensitive data</Th>
              {canEdit && <Th>{''}</Th>}
            </Tr>
          </thead>
          <tbody className="divide-y divide-border">
            {data.sites.map((row) => (
              <tr key={row.site} data-testid="host-site">
                <Td density="cozy" tone="foreground" className="align-top">
                  {siteName(row.site)}
                  <div className="text-[11px] text-muted-foreground">internal name: <span className="font-mono">{row.key}</span></div>
                  {row.description && <div className="text-[11px] text-muted-foreground">{row.description}</div>}
                  <details className="text-[11px] text-muted-foreground">
                    <summary className="cursor-pointer">What it may do here</summary>
                    <ul className="m-0 list-disc pl-5 text-foreground-secondary">
                      {describeProfile(row).map((l) => <li key={l} className="break-words">{l}</li>)}
                    </ul>
                  </details>
                  {editing === row.site && (
                    <SiteForm
                      row={row}
                      busy={busy}
                      onCancel={() => setEditing(null)}
                      onSave={(fields) => void run(() => putAgentHostSite(agentSlug, row.site, fields)).then((saved) => saved && setEditing(null))}
                    />
                  )}
                </Td>
                <Td density="cozy" tone="secondary" mono className="align-top text-[11px]">
                  {(row.ceiling ?? []).length ? (row.ceiling ?? []).map((g) => <div key={g}>{g}</div>) : <span className="font-sans">whatever the site allows</span>}
                </Td>
                <Td density="cozy" tone="secondary" mono className="align-top text-[11px]">
                  {(row.own_login ?? []).length ? (row.own_login ?? []).map((n) => <div key={n}>{n}</div>) : <span className="font-sans">none</span>}
                </Td>
                <Td density="cozy" className="align-top">
                  <div className="flex flex-wrap items-center gap-2">
                    {row.sensitive ? (
                      <span data-testid="handles-sensitive" className="rounded bg-warning/10 px-1.5 py-0.5 font-medium text-warning">On</span>
                    ) : (
                      <span className="text-muted-foreground">Off</span>
                    )}
                    {canEdit && (
                      <button
                        type="button"
                        role="switch"
                        aria-checked={Boolean(row.sensitive)}
                        aria-label={`Handles sensitive data on ${siteName(row.site)}`}
                        disabled={busy}
                        className={QUIET}
                        onClick={() => void run(() => putAgentHostSite(agentSlug, row.site, { sensitive: !row.sensitive }))}
                      >
                        {row.sensitive ? 'Turn off' : 'Turn on'}
                      </button>
                    )}
                  </div>
                </Td>
                {canEdit && (
                  <Td density="cozy" className="align-top whitespace-nowrap">
                    <button type="button" className={QUIET} aria-label={`Change ${siteName(row.site)}`} onClick={() => setEditing(row.site)}>Change</button>{' '}
                    <button type="button" disabled={busy} className={DANGER} aria-label={`Remove ${siteName(row.site)}`} onClick={() => void run(() => deleteAgentHostSite(agentSlug, row.site))}>Remove</button>
                  </Td>
                )}
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <p className="m-0 text-[11px] text-muted-foreground">
        Turned on, a conversation on that site runs only in its confined list, for everyone including the owner. Who may use {agentName} from a site is on the Roles tab.
      </p>
      {canEdit && addable.length > 0 && (
        <form
          className="flex items-center gap-2"
          onSubmit={(e) => {
            e.preventDefault()
            if (adding) void run(() => putAgentHostSite(agentSlug, adding, {})).then((saved) => saved && setAdding(''))
          }}
        >
          <select aria-label="Site to add" value={adding} onChange={(e) => setAdding(e.target.value)} className={FIELD}>
            <option value="">Pick a site…</option>
            {addable.map((s) => <option key={s} value={s}>{siteName(s)}</option>)}
          </select>
          <button type="submit" disabled={busy || !adding} className={BUTTON}>Add site</button>
        </form>
      )}
      <Failure error={error} />
    </div>
  )
}

function changeLabel(c: SettingChangeOut): string {
  // reset_interface and set_capability_sensitive are retired routes; old history rows still carry them.
  switch (c.operation) {
    case 'put_read_mode': return 'Read mode customised'
    case 'reset_read_mode':
    case 'reset_interface': return 'Back to the default'
    case 'put_agent_host_site': return 'Site changed'
    case 'delete_agent_host_site': return 'Site removed'
    case 'set_capability_sensitive': return 'Sensitive data changed'
    default: return 'Saved'
  }
}

/** Read mode and host sites, with who changed them. */
export function AgentAnsweringSettings({ agentSlug, agentName, canEdit = false }: {
  agentSlug: string; agentName?: string; canEdit?: boolean
}) {
  const [history, setHistory] = useState<SettingChangeOut[] | null>(null)
  const [version, setVersion] = useState(0)
  const name = agentName ?? agentSlug
  const changed = () => setVersion((v) => v + 1)

  // Best effort: without it the settings still show.
  useEffect(() => {
    let cancelled = false
    getAgentSettingsHistory(agentSlug)
      .then((h) => !cancelled && setHistory(h))
      .catch(() => !cancelled && setHistory([]))
    return () => { cancelled = true }
  }, [agentSlug, version])

  return (
    <div className="flex flex-col gap-2">
      <ReadModeCard agentSlug={agentSlug} agentName={name} canEdit={canEdit} onChange={changed} />
      <HostSitesCard agentSlug={agentSlug} agentName={name} canEdit={canEdit} onChange={changed} />
      {history && history.length > 0 && (
        <div data-testid="settings-history" className="flex flex-col gap-1">
          <div className="text-[12px] font-medium text-foreground">Change history</div>
          <table className="w-full text-[12px]">
            <thead>
              <Tr head="small">
                <Th>When</Th>
                <Th>Who</Th>
                <Th>Through</Th>
                <Th>Change</Th>
              </Tr>
            </thead>
            <tbody className="divide-y divide-border">
              {history.slice(0, 10).map((c) => (
                <tr key={c.id}>
                  <Td tone="secondary">{new Date(c.created_at).toLocaleString()}</Td>
                  <Td tone="secondary">{c.actor_email || 'someone'}</Td>
                  <Td tone="secondary">{c.via}</Td>
                  <Td tone="secondary">{changeLabel(c)}</Td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
