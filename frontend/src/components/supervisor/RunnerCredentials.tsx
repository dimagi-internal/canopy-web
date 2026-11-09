import { useCallback, useEffect, useRef, useState, type JSX } from 'react'
import { cliLabel } from '@/lib/clis'
import {
  addRunnerLogin,
  addVaultedRunnerCredential,
  createRunnerVault,
  getRunnerCredentialStatus,
  orderRunnerCredentials,
  removeRunnerCredential,
  renameRunnerCredential,
  type CredentialKind,
  type CredentialStatus,
  type RunnerCredentialEntry,
} from '@/api/harness'
import { sealWorkspaceVaultValue } from '@/vault/operations'
import { useVaultUnlock } from '@/vault/useVaultUnlock'
import { RunnerReauth } from './RunnerReauth'
import { RunnerVaultRow } from '@/components/supervisor/RunnerVaultRow'
import { CredentialSetRow } from '@/components/supervisor/CredentialSetRow'
import { CredentialName, NewApiKey, ReplaceKey, smallButton } from '@/components/supervisor/CredentialKeyForms'

export type { CredentialStatus }

// A cloud runner's Claude credentials: ONE ordered list of logins, tokens and
// API keys, any number of each. The runner uses the first that works and moves
// down when one hits its usage cap or stops working.
//
// Values are WRITE-ONLY here by construction: the status endpoint returns
// booleans and names, never a value, so this component can say "set" and can
// never render a token or key.
//
// The words stay short on purpose. What each kind of Claude credential can and
// cannot do is still moving (connectors, Claude Design), so the page names the
// kinds and leaves the detail to Anthropic's docs rather than restate it wrongly.

export const KIND_LABEL: Record<CredentialKind, string> = {
  login: 'Login',
  token: 'Token',
  api_key: 'API key',
}

const KIND_HINT: Record<CredentialKind, string> = {
  login: 'Signs this runner in to a Claude account.',
  token: 'From claude setup-token. Valid for a year.',
  api_key: 'An Anthropic API key. Billed per use.',
}

/** Whether an entry can run a turn as far as canopy-web knows: a login the box
 *  says is signed in, or a token or key that holds a value. */
export function usable(e: RunnerCredentialEntry): boolean {
  return e.kind === 'login' ? e.signed_in : e.has_value
}

/** The one warning this block shows: nothing on the list can run a turn. */
export function credentialWarning(s: CredentialStatus, signedIn: boolean | null = null): string | null {
  return (s.credentials ?? []).some(usable) || signedIn
    ? null
    : 'No working credential: this runner cannot run a turn. Add one below.'
}

/** The account the box's own login belongs to, read out of its `claude.login`
 *  health check ("signed in on this box as <email> — …"). The box knows who it
 *  signed in as, so the sign-in row shows THAT rather than a name someone types:
 *  a hand-typed label is how a row came to name one account while holding
 *  another. Empty for an older runner that does not say. */
export function boxLoginAccount(detail: string | undefined): string {
  return detail?.match(/signed in on this box as (\S+@\S+)/)?.[1] ?? ''
}

/** The label the box reports an entry's probe under (cloud_runner.py
 *  `_cascade_from_list`, `_login_label`). */
export function probeLabel(e: Pick<RunnerCredentialEntry, 'kind' | 'key'>): string {
  if (e.kind === 'login') return e.key === 'default' ? 'box-login' : `box-login:${e.key}`
  return `${e.kind === 'token' ? 'token' : 'api-key'}:${e.key}`
}

export interface ProbeCheck {
  name?: string
  status: string
  detail?: string
}

export type ProbeTone = 'ok' | 'warn' | 'fail' | null

/** What the box last proved about one credential: its `claude.probe.<label>`
 *  health check. Null tone when the box has not probed it (an older runner, or
 *  not yet) — the page then says only that a value is set. */
export function probeFor(
  checks: Record<string, ProbeCheck> | null | undefined,
  label: string,
): { tone: ProbeTone; detail: string } {
  const check = checks?.[`claude.probe.${label}`]
  if (!check) return { tone: null, detail: '' }
  const tone: ProbeTone = check.status === 'ok' ? 'ok' : check.status === 'fail' ? 'fail' : 'warn'
  return { tone, detail: check.detail ?? '' }
}

/** The dot's colour: what the probe found, else whether a value is held. */
function dotClass(tone: ProbeTone, held: boolean): string {
  if (tone === 'ok') return 'text-success'
  if (tone === 'warn') return 'text-warning'
  if (tone === 'fail') return 'text-destructive'
  return held ? 'text-success' : 'text-muted-foreground'
}

function probeTextClass(tone: ProbeTone): string {
  return tone === 'fail' ? 'text-destructive' : tone === 'warn' ? 'text-warning' : 'text-foreground-subtle'
}

/** Move `key` to `to` in `keys` — the new order to send. */
export function moved(keys: string[], key: string, to: number): string[] {
  const rest = keys.filter((k) => k !== key)
  const i = Math.max(0, Math.min(to, rest.length))
  return [...rest.slice(0, i), key, ...rest.slice(i)]
}

export function RunnerCredentials({ runnerId, workspace = null, signedIn = null, signedInAs = '', healthChecks = null, apiKeysOnly = false }: {
  runnerId: string
  /** The runner's workspace, where its vault's page lives. */
  workspace?: string | null
  /** The runner's health checks, for each credential's `claude.probe.<label>`. */
  healthChecks?: Record<string, ProbeCheck> | null
  /** Whether the box reports a full Claude sign-in (its `claude.login` health
   *  check). Null when the runner has not said. */
  signedIn?: boolean | null
  /** Which account that sign-in is (see boxLoginAccount); '' when unknown. */
  signedInAs?: string
  /** A sensitive-data box uses its ZDR API key and nothing else, so only API keys can be added. */
  apiKeysOnly?: boolean
}): JSX.Element {
  const [status, setStatus] = useState<CredentialStatus | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [adding, setAdding] = useState<CredentialKind | null>(null)
  const [dragging, setDragging] = useState<string | null>(null)
  const [dropTarget, setDropTarget] = useState<string | null>(null)
  const alive = useRef(true)
  const unlocker = useVaultUnlock()
  const { unlock } = unlocker

  const load = useCallback(async () => {
    try {
      const s = await getRunnerCredentialStatus(runnerId)
      if (alive.current) setStatus(s)
    } catch (e) {
      if (alive.current) setError(e instanceof Error ? e.message : 'Failed to load')
    }
  }, [runnerId])

  useEffect(() => {
    alive.current = true
    void load()
    return () => {
      alive.current = false
    }
  }, [load])

  const write = async (fn: () => Promise<CredentialStatus>, fallback: string) => {
    setBusy(true)
    setError(null)
    try {
      const s = await fn()
      if (alive.current) setStatus(s)
      return true
    } catch (e) {
      if (alive.current) setError(e instanceof Error ? e.message : fallback)
      return false
    } finally {
      if (alive.current) setBusy(false)
    }
  }

  if (status === null && error === null) {
    return <div className="h-6 w-48 animate-pulse rounded-md bg-muted" data-testid="runner-credentials-loading" />
  }

  const entries = status?.credentials ?? []
  const keys = entries.map((e) => e.key)
  const warning = status ? credentialWarning(status, signedIn) : null
  // A vault with a key: a pasted value is sealed into it in this browser, never sent.
  const openVault = (status?.vaults ?? []).find((v) => v.kv > 0) ?? null

  /** Run a vault step, then reload the list. */
  const inVault = async (fn: () => Promise<unknown>, fallback: string) =>
    write(async () => { await fn(); return getRunnerCredentialStatus(runnerId) }, fallback)
  // A token's or key's value goes only into the runner's vault, sealed in this browser
  // (or by the box after a sign-in) — so without a vault key there is nowhere to put one.
  const addKey = (value: string, name: string) => openVault
    ? inVault(async () => {
      const entry = await addVaultedRunnerCredential(runnerId, 'api_key', name)
      await sealWorkspaceVaultValue(entry.vault_id, entry.secret_name, value, await unlock())
    }, 'Failed to seal the key into the vault')
    : Promise.resolve(false)
  const replaceKey = (e: RunnerCredentialEntry, value: string) => openVault
    ? inVault(async () => sealWorkspaceVaultValue(openVault.vault_id, e.vault_secret, value, await unlock()),
      'Failed to seal the key into the vault')
    : Promise.resolve(false)
  /** A kind whose value lives in the vault: none can be added before its key exists. */
  const needsVault = (kind: CredentialKind) => kind !== 'login' && !openVault

  const reorder = (key: string, to: number) => {
    const next = moved(keys, key, to)
    if (next.join() === keys.join()) return
    return write(() => orderRunnerCredentials(runnerId, next), 'Failed to reorder')
  }
  const remove = (e: RunnerCredentialEntry) => {
    const what = e.account || e.name || KIND_LABEL[e.kind].toLowerCase()
    if (!window.confirm(`Remove ${what}? The runner stops using it within a few minutes.`)) return
    return write(() => removeRunnerCredential(runnerId, e.key), 'Failed to remove')
  }
  const startAdding = (kind: CredentialKind) => {
    if (kind === 'login') {
      setAdding(null)
      return void write(() => addRunnerLogin(runnerId), 'Failed to add a login')
    }
    setAdding(adding === kind ? null : kind)
  }
  const addable: CredentialKind[] = apiKeysOnly ? ['api_key'] : ['login', 'token', 'api_key']

  return (
    <div className="flex flex-col gap-2 rounded-lg border border-border bg-card p-3" data-testid="runner-credentials">
      <div className="flex items-center gap-2">
        <span className="text-[11px] uppercase tracking-wide text-muted-foreground">Credentials</span>
        {status?.updated_at && (
          <span className="ml-auto text-[11px] text-foreground-subtle">
            updated {new Date(status.updated_at).toLocaleDateString()}
          </span>
        )}
      </div>

      {warning && (
        <p className="rounded-md border border-warning/40 bg-warning/10 px-2 py-1 text-[12px] text-warning" data-testid="runner-credentials-warning">
          ⚠ {warning}
        </p>
      )}

      {status && (
        <RunnerVaultRow
          runnerId={runnerId}
          workspace={workspace}
          vaults={status.vaults ?? []}
          busy={busy}
          run={inVault}
          create={() => createRunnerVault(runnerId)}
          unlock={unlock}
        />
      )}

      {status && (
        <CredentialSetRow
          runnerId={runnerId}
          status={status}
          busy={busy}
          write={write}
        />
      )}

      <p className="text-[11px] text-foreground-subtle">
        The runner uses the first credential that works, top to bottom.
        {apiKeysOnly && ' This is a sensitive-data runner: it uses only its ZDR API key.'}
      </p>

      {/* A box signed in before there was a list: the runner adds it here on
          its next re-read. Until then, say what the box says. */}
      {signedIn && !entries.some((e) => e.kind === 'login') && (
        <div className="flex items-center gap-2 rounded-md border border-border p-2 text-[12px]" data-testid="cred-login-pending-adopt">
          <span className="text-success">●</span>
          <span className="text-foreground">{signedInAs || 'signed in on this runner'}</span>
          <span className="text-[11px] text-foreground-subtle">joins this list when the runner next checks in</span>
        </div>
      )}

      {entries.length > 0 && (
        <ol className="flex flex-col gap-1.5" data-testid="cred-list">
          {entries.map((e, i) => {
            const probe = probeFor(healthChecks, probeLabel(e))
            const held = usable(e)
            const dropHere = dragging && dragging !== e.key && dropTarget === e.key
            return (
              <li
                key={e.key}
                data-testid={`cred-${e.key}`}
                onDragOver={(ev) => {
                  if (dragging && dragging !== e.key) {
                    ev.preventDefault()
                    setDropTarget(e.key)
                  }
                }}
                onDragLeave={() => setDropTarget(null)}
                onDrop={(ev) => {
                  ev.preventDefault()
                  const from = dragging
                  setDragging(null)
                  setDropTarget(null)
                  if (from && from !== e.key) void reorder(from, i)
                }}
                className={`flex flex-col gap-1.5 rounded-md border p-2 ${
                  dropHere ? 'border-primary bg-primary/5' : 'border-border'
                } ${dragging === e.key ? 'opacity-50' : ''}`}
              >
                <div className="flex items-center gap-2 text-[12px] text-foreground">
                  {/* Only the handle drags: a draggable row would steal the mouse
                      from its own controls. The whole row is the drop target. */}
                  <span
                    aria-hidden
                    draggable={!busy && entries.length > 1}
                    onDragStart={(ev) => {
                      ev.dataTransfer.effectAllowed = 'move'
                      ev.dataTransfer.setData('text/plain', e.key)
                      const row = ev.currentTarget.closest('li')
                      if (row) ev.dataTransfer.setDragImage(row, 12, 12)
                      setDragging(e.key)
                    }}
                    onDragEnd={() => { setDragging(null); setDropTarget(null) }}
                    title={entries.length > 1 ? 'Drag to reorder' : undefined}
                    className={`select-none text-foreground-subtle ${entries.length > 1 ? 'cursor-grab' : 'opacity-40'}`}
                  >
                    ⋮⋮
                  </span>
                  <span className="w-4 text-right tabular-nums text-foreground-subtle">{i + 1}</span>
                  <span
                    className={dotClass(held ? probe.tone : null, held)}
                    title={probe.detail || undefined}
                    data-testid={`cred-dot-${e.key}`}
                  >
                    {held ? '●' : '○'}
                  </span>
                  <span
                    className="w-14 shrink-0 rounded bg-muted px-1.5 py-0.5 text-center text-[11px] text-foreground-secondary"
                    data-testid={`cred-kind-${e.key}`}
                  >
                    {KIND_LABEL[e.kind]}
                  </span>
                  {e.cli && e.cli !== 'claude-code' && (
                    <span className="shrink-0 rounded bg-muted px-1.5 py-0.5 text-[11px] text-foreground-secondary"
                      data-testid={`cred-cli-${e.key}`}>{cliLabel(e.cli)}</span>
                  )}
                  {e.in_vault && (
                    <span className="text-[11px] text-foreground-subtle" title="Sealed in the runner's vault: only the box's own key opens it"
                      data-testid={`cred-in-vault-${e.key}`}>vault</span>
                  )}
                  {e.kind === 'login' ? (
                    <span className="truncate" data-testid={`cred-account-${e.key}`}>
                      {e.signed_in ? (e.account || 'signed in') : e.account ? `${e.account} (signed out)` : 'not signed in yet'}
                    </span>
                  ) : (
                    <CredentialName
                      entryKey={e.key}
                      name={e.name}
                      disabled={busy}
                      onSave={(name) => write(() => renameRunnerCredential(runnerId, e.key, name), 'Failed to rename')}
                    />
                  )}
                  <span className="ml-auto flex items-center gap-0.5">
                    {/* Buttons as well as dragging: HTML drag-and-drop does nothing on a phone. */}
                    <button type="button" onClick={() => void reorder(e.key, i - 1)} disabled={busy || i === 0}
                      data-testid={`cred-up-${e.key}`} aria-label="Move up" className={smallButton}>↑</button>
                    <button type="button" onClick={() => void reorder(e.key, i + 1)} disabled={busy || i === entries.length - 1}
                      data-testid={`cred-down-${e.key}`} aria-label="Move down" className={smallButton}>↓</button>
                    <button type="button" onClick={() => void remove(e)} disabled={busy}
                      data-testid={`cred-remove-${e.key}`}
                      className="rounded-md px-2 py-0.5 text-[11px] text-muted-foreground hover:bg-muted hover:text-destructive disabled:opacity-40">
                      Remove
                    </button>
                  </span>
                </div>
                {held && probe.detail && probe.tone !== 'ok' && (
                  <span className={`text-[11px] ${probeTextClass(probe.tone)}`} data-testid={`cred-probe-${e.key}`}>
                    {probe.detail}
                  </span>
                )}
                {e.kind === 'login' && (
                  <RunnerReauth runnerId={runnerId} slot="box" loginKey={e.key === 'default' ? '' : e.key}
                    isSet={e.signed_in} onSignedIn={load} />
                )}
                {e.kind === 'token' && (
                  <RunnerReauth runnerId={runnerId} slot="token" loginKey={e.key} isSet={e.has_value} onSignedIn={load} />
                )}
                {e.kind === 'api_key' && (
                  <ReplaceKey disabled={busy || !openVault} entryKey={e.key} isSet={e.has_value}
                    onSave={(value) => replaceKey(e, value)} />
                )}
              </li>
            )
          })}
        </ol>
      )}

      <div className="flex flex-wrap gap-1.5 pt-1">
        {addable.map((kind) => (
          <button
            key={kind}
            type="button"
            onClick={() => startAdding(kind)}
            disabled={busy || needsVault(kind)}
            title={needsVault(kind) ? 'Make the vault’s key first: its value is kept only there' : KIND_HINT[kind]}
            data-testid={`cred-add-${kind}`}
            className={`rounded-md border border-dashed px-2.5 py-1 text-[12px] hover:bg-muted hover:text-foreground disabled:opacity-40 ${
              adding === kind ? 'border-primary text-foreground' : 'border-border text-muted-foreground'
            }`}
          >
            + {KIND_LABEL[kind]}
          </button>
        ))}
      </div>
      {!openVault && addable.some(needsVault) && (
        <p className="text-[11px] text-foreground-subtle" data-testid="cred-needs-vault">
          Tokens and API keys are kept only in this runner&rsquo;s vault, so make its key (above) before adding one.
        </p>
      )}
      <ul className="flex flex-col text-[11px] text-foreground-subtle">
        {addable.map((kind) => (
          <li key={kind}><span className="text-foreground-secondary">{KIND_LABEL[kind]}:</span> {KIND_HINT[kind]}</li>
        ))}
      </ul>

      {adding === 'token' && (
        <div className="rounded-md border border-dashed border-border p-2" data-testid="cred-new-token">
          <RunnerReauth runnerId={runnerId} slot="token" autoStart onSignedIn={() => { setAdding(null); void load() }} />
        </div>
      )}
      {adding === 'api_key' && (
        <NewApiKey
          disabled={busy}
          onCancel={() => setAdding(null)}
          onSave={async (value, name) => {
            if (await addKey(value, name)) {
              setAdding(null)
              return true
            }
            return false
          }}
        />
      )}

      {error && <span className="text-[12px] text-destructive" data-testid="runner-credentials-error">{error}</span>}

      <p className="text-[11px] text-foreground-subtle">
        Tokens and keys are sealed in this runner&rsquo;s vault: this page shows whether one is set, never what it is.
      </p>
      {unlocker.element}
    </div>
  )
}

