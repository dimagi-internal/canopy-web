import { useEffect, useState } from 'react'
import {
  getAgentInterface,
  getAgentRepoHead,
  saveAgentInterface,
  unpublishAgentInterface,
  type AgentInterfaceOut,
  type AgentRepoHeadOut,
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

const SHA = /^[0-9a-f]{40}$/
// A `data: phi` line, the block form an author writes; JSON (`"data": "phi"`)
// is caught by PHI_ANYWHERE but never edited.
const PHI_LINE = /^\s*data\s*:\s*["']?phi["']?\s*(#.*)?$/
const PHI_ANYWHERE = /(^|[\s{,])["']?data["']?\s*:\s*["']?phi\b/m

// Does this YAML declare any capability PHI-safe? Comments do not count. The
// server parses it for real; this only decides whether to ask for a commit.
export function declaresPhi(source: string): boolean {
  return PHI_ANYWHERE.test(source.split('\n').map((l) => l.replace(/(^|\s)#.*$/, '')).join('\n'))
}

// The same YAML with every `data: phi` line taken out: saving it withdraws.
function withoutPhi(source: string): string {
  return source.split('\n').filter((l) => !PHI_LINE.test(l)).join('\n')
}

const short = (sha: string) => sha.slice(0, 12)

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
  // The commit a `data: phi` save declares, and the repo head offered for it.
  const [declareSha, setDeclareSha] = useState('')
  const [shaTouched, setShaTouched] = useState(false)
  const [head, setHead] = useState<AgentRepoHeadOut | null>(null)
  const [headError, setHeadError] = useState<string | null>(null)

  const declared = data?.declarations ?? []
  const draftPhi = draft !== null && declaresPhi(draft)
  // The head is read only for an admin, and only when there is a declaration to
  // compare it with or one about to be made.
  const wantHead = canEdit && (draftPhi || (draft === null && declared.length > 0))

  useEffect(() => {
    if (!wantHead || head || headError) return
    let cancelled = false
    getAgentRepoHead(agentSlug)
      .then((h) => !cancelled && setHead(h))
      .catch((e: unknown) => !cancelled && setHeadError(e instanceof Error ? e.message : 'Could not read the repo'))
    return () => {
      cancelled = true
    }
  }, [wantHead, head, headError, agentSlug])

  // Offer the head as the commit, unless the person has typed one.
  useEffect(() => {
    if (draftPhi && head?.sha && !shaTouched) setDeclareSha(head.sha)
  }, [draftPhi, head, shaTouched])

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
      setDeclareSha('')
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Could not save')
    } finally {
      setBusy(false)
    }
  }

  const startEdit = (opts: { withdraw?: boolean; sha?: string } = {}) => {
    const iface = data?.interface ?? {}
    // JSON is valid YAML, so an interface saved as a mapping still round-trips.
    const source = data?.source || (Object.keys(iface).length ? JSON.stringify(iface, null, 2) : STARTER)
    setDraft(opts.withdraw ? withoutPhi(source) : source)
    setDeclareSha(opts.sha ?? '')
    setShaTouched(Boolean(opts.sha))
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
          <div data-testid="phi-declare" className="flex flex-col gap-1 rounded-md border border-warning/30 bg-warning/10 p-2">
            <p className="m-0 text-[12px] text-foreground">
              This declares every <code>data: phi</code> capability PHI-safe: you vouch that, at this commit, it reads
              a host&apos;s data only through canopy&apos;s gateway and keeps none of it — and runners run exactly this
              commit for it.
            </p>
            <label htmlFor={`iface-sha-${agentSlug}`} className="text-[12px] text-muted-foreground">
              Commit the declaration covers
            </label>
            <input
              id={`iface-sha-${agentSlug}`}
              value={declareSha}
              onChange={(e) => {
                setShaTouched(true)
                setDeclareSha(e.target.value.trim())
              }}
              placeholder="40-character commit SHA"
              spellCheck={false}
              className="w-full rounded-md border border-input bg-input px-2 py-1 font-mono text-[12px] text-foreground"
            />
            <p data-testid="phi-declare-head" className="m-0 text-[11px] text-muted-foreground">
              {head?.sha ? (
                <>
                  Current head of {head.repo}@{head.ref}
                  {declareSha && declareSha !== head.sha && ' is ' + short(head.sha) + ' — you are declaring another commit'}.
                </>
              ) : head || headError ? (
                `Canopy could not read the agent repo's head (${head?.detail || headError}): paste the commit.`
              ) : (
                'Reading the agent repo’s head…'
              )}
            </p>
            {declareSha && !SHA.test(declareSha) && (
              <p role="alert" className="m-0 text-[11px] text-destructive">
                A full 40-character commit SHA, lower-case hex.
              </p>
            )}
          </div>
        )}
        {!draftPhi && declared.length > 0 && (
          <p data-testid="phi-withdraw" className="m-0 rounded-md border border-warning/30 bg-warning/10 px-2 py-1 text-[12px] text-warning">
            No capability says <code>data: phi</code> any more: saving withdraws the PHI-safe declaration of{' '}
            {declared.map((d) => d.capability).join(', ')}, and hosts stop giving it their data.
          </p>
        )}
        <div className="flex flex-wrap items-center gap-2">
          <button
            type="button"
            disabled={busy || (draftPhi && !SHA.test(declareSha))}
            onClick={() =>
              void run(() =>
                draftPhi ? saveAgentInterface(agentSlug, draft, declareSha) : saveAgentInterface(agentSlug, draft),
              )
            }
            className="rounded-md bg-primary px-3 py-1 text-[12px] font-medium text-primary-foreground disabled:opacity-50"
          >
            {busy
              ? 'Saving…'
              : draftPhi
                ? 'Save and declare'
                : declared.length > 0
                  ? 'Save and withdraw'
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
                </tr>
              )
            })}
          </tbody>
        </table>
      )}
      {declared.length > 0 && (
        <div data-testid="interface-declarations" className="flex flex-col gap-1">
          <div className="text-[12px] font-medium text-foreground">Declared PHI-safe</div>
          <table className="w-full text-[13px]">
            <thead>
              <tr className="text-left text-[11px] text-muted-foreground">
                <th className="py-1 font-medium">Capability</th>
                <th className="py-1 font-medium">Data</th>
                <th className="py-1 font-medium">Commit</th>
                <th className="py-1 font-medium">Declared by</th>
                <th className="py-1 font-medium">Declared</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {declared.map((d) => (
                <tr key={d.capability} data-testid="interface-declaration">
                  <td className="py-1.5 font-mono text-foreground">{d.capability}</td>
                  <td className="py-1.5 font-mono text-[12px] text-foreground-secondary">data: {d.data}</td>
                  <td className="py-1.5 font-mono text-[12px]">
                    {d.commit_url ? (
                      <a href={d.commit_url} target="_blank" rel="noreferrer" className="text-primary underline">
                        {short(d.sha)}
                      </a>
                    ) : (
                      short(d.sha)
                    )}
                  </td>
                  <td className="py-1.5 text-[12px] text-foreground-secondary">{d.declared_by_email ?? 'someone'}</td>
                  <td className="py-1.5 text-[12px] text-foreground-secondary">
                    {new Date(d.declared_at).toLocaleString()}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {declared.map((d) =>
            d.last_run_sha != null && d.last_run_sha !== d.sha ? (
              <p
                key={`ran-${d.capability}`}
                data-testid="declaration-mismatch"
                role="alert"
                className="m-0 rounded-md border border-destructive/30 bg-destructive/10 px-2 py-1 text-[12px] text-destructive"
              >
                The last turn in <code>{d.capability}</code>
                {d.last_run_at ? ` (${new Date(d.last_run_at).toLocaleString()})` : ''} ran{' '}
                {d.last_run_sha ? <code>{short(d.last_run_sha)}</code> : 'no declared commit'}, not the declared{' '}
                <code>{short(d.sha)}</code>: its Canopy-Execution statement carried no declaration for it, so hosts
                withheld their PHI. Turns claimed from now on run <code>{short(d.sha)}</code>.
              </p>
            ) : null,
          )}
          {head?.sha &&
            declared.map((d) =>
              d.sha !== head.sha ? (
                <div
                  key={`head-${d.capability}`}
                  data-testid="declaration-behind"
                  className="flex flex-wrap items-center gap-2 rounded-md border border-warning/30 bg-warning/10 px-2 py-1 text-[12px] text-warning"
                >
                  <span>
                    {head.repo}@{head.ref} is at <code>{short(head.sha)}</code>, not the declared{' '}
                    <code>{short(d.sha)}</code>. Turns in <code>{d.capability}</code> keep running the declared commit —
                    not the newer code — until you declare again.
                  </span>
                  {canEdit && (
                    <button
                      type="button"
                      onClick={() => startEdit({ sha: head.sha })}
                      className="rounded-md border border-border bg-input px-2 py-0.5 text-[12px] font-medium text-foreground hover:border-primary"
                    >
                      Declare {short(head.sha)}
                    </button>
                  )}
                </div>
              ) : null,
            )}
          <p className="m-0 text-[11px] text-muted-foreground">
            To withdraw, remove <code>data: phi</code> from the capability and save — or unpublish the interface.
          </p>
        </div>
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
          {declared.length > 0 && (
            <button
              type="button"
              onClick={() => startEdit({ withdraw: true })}
              className="rounded-md border border-border bg-input px-3 py-1 text-[12px] font-medium text-foreground hover:border-destructive"
            >
              Withdraw PHI declaration
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
    </div>
  )
}
