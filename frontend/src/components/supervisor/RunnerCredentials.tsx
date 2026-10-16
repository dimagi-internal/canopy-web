import { useCallback, useEffect, useRef, useState, type JSX } from 'react'
import {
  addRunnerLogin,
  getRunnerCredentialStatus,
  orderRunnerLogins,
  removeRunnerLogin,
  setRunnerCredential,
  swapRunnerLogins,
  type CredentialStatus,
  type LoginSlot,
  type RunnerLogin,
} from '@/api/harness'
import { RunnerReauth } from './RunnerReauth'

export type { CredentialStatus }

// Set a cloud runner's credentials from the browser.
//
// `POST /runners/{id}/credential` and `GET /credential/status` have existed since
// the runner-credential work; there was no page, so the only way to set a token
// was a terminal on someone's machine. Meanwhile every boot of the cloud box
// logged:
//
//   warn: only ONE Claude credential is set — a usage cap will stop every agent
//   on this box with nothing to fail over to
//
// That is a fleet-wide outage waiting on a form. This is the form.
//
// Values are WRITE-ONLY here by construction: the status endpoint returns
// booleans and never plaintext, so this component can say "set" and "when", and
// can never render a token. A field left blank is not sent at all — the write
// schema is non-clobbering, and sending "" would wipe a working credential.

export type SlotKey =
  | 'claude_token'
  | 'claude_token_secondary'
  | 'claude_api_key'

export interface Slot {
  key: SlotKey
  statusKey: `has_${SlotKey}`
  label: string
  hint: string
  /** A subscription LOGIN: it can be signed in from here, named, and reordered.
   *  The API key is none of those — it is pasted, and it is always last. */
  login?: { slot: LoginSlot; labelKey: LabelKey }
}

export type LabelKey = 'claude_token_label' | 'claude_token_secondary_label'

// Claude auth is an ORDERED CASCADE, not one credential — a subscription has a
// weekly cap, and when it trips every agent on the box stops. Order here is the
// order the runner falls through them.
export const SLOTS: readonly Slot[] = [
  {
    key: 'claude_token',
    statusKey: 'has_claude_token',
    label: 'Backup token 1',
    hint: 'Used when the Claude logins above are capped or stop working. Good for a year with nothing to renew; cannot use claude.ai connectors such as Claude Docs.',
    login: { slot: 'primary', labelKey: 'claude_token_label' },
  },
  {
    key: 'claude_token_secondary',
    statusKey: 'has_claude_token_secondary',
    label: 'Backup token 2',
    hint: 'Tried after backup token 1. Same limits: no claude.ai connectors.',
    login: { slot: 'secondary', labelKey: 'claude_token_secondary_label' },
  },
  {
    key: 'claude_api_key',
    statusKey: 'has_claude_api_key',
    label: 'Claude API key (last resort)',
    hint: 'Metered, deliberately — falling back this far should notify a human rather than quietly spend money.',
  },
]
// No GitHub slot: a box holds no GitHub credential. Each agent's turns get its
// owner's token for that agent, one turn at a time — set on the agent's own
// Settings → Credentials → GitHub.

/** Only the slots actually typed into, trimmed. Blank means "leave alone", never
 *  "clear" — the write schema is non-clobbering and "" would overwrite. */
export function nextPayload(draft: Partial<Record<SlotKey, string>>): Partial<Record<SlotKey, string>> {
  const out: Partial<Record<SlotKey, string>> = {}
  for (const [k, v] of Object.entries(draft)) {
    const trimmed = (v ?? '').trim()
    if (trimmed) out[k as SlotKey] = trimmed
  }
  return out
}

export interface Summary {
  claudeFallback: 'none' | 'secondary' | 'api_key'
  warning: string | null
  unset: SlotKey[]
}

export function credentialSummary(s: CredentialStatus, signedIn: boolean | null = null): Summary {
  const unset = SLOTS.filter((slot) => !s[slot.statusKey]).map((slot) => slot.key)
  const fallback: Summary['claudeFallback'] = s.has_claude_token_secondary
    ? 'secondary'
    : s.has_claude_api_key
      ? 'api_key'
      : 'none'

  // ONLY the state that means this box cannot run anything. The two
  // fallback warnings that used to live here — "only one credential is set",
  // "the only fallback is metered" — were removed 2026-09-08: a standing banner
  // about a deliberate configuration is a nag, and it sat above the controls
  // every single visit, training the eye to skip the whole block. Which is
  // exactly where the one alarm that matters has to be seen.
  //
  // `claudeFallback` is still computed; a caller that wants to say something
  // about fallbacks can, at a moment when it is actually the subject.
  const warning: string | null = s.has_claude_token || signedIn
    || (s.logins ?? []).some((l) => l.signed_in)
    ? null
    : 'No Claude credential at all — this runner cannot execute any turn.'
  return { claudeFallback: fallback, warning, unset }
}

/** The account the box's own login belongs to, read out of its `claude.login`
 *  health check ("signed in on this box as <email> — …"). The box knows who it
 *  signed in as, so the sign-in row shows THAT rather than a name someone types:
 *  a hand-typed label is how a row came to name one account while holding
 *  another (2026-10-14). Empty for an older runner that does not say. */
export function boxLoginAccount(detail: string | undefined): string {
  return detail?.match(/signed in on this box as (\S+@\S+)/)?.[1] ?? ''
}

export function RunnerCredentials({ runnerId, signedIn = null, signedInAs = '' }: {
  runnerId: string
  /** Whether the box reports a full Claude sign-in (its `claude.login` health
   *  check). Null when the runner has not said — an older runner, or no
   *  heartbeat yet. */
  signedIn?: boolean | null
  /** Which account that sign-in is (see boxLoginAccount); '' when unknown. */
  signedInAs?: string
}): JSX.Element {
  const [status, setStatus] = useState<CredentialStatus | null>(null)
  const [apiKey, setApiKey] = useState('')
  const [dragging, setDragging] = useState<LoginSlot | null>(null)
  const [dropTarget, setDropTarget] = useState<LoginSlot | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [saved, setSaved] = useState(false)
  const alive = useRef(true)

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
    setSaved(false)
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

  const saveApiKey = async () => {
    const payload = nextPayload({ claude_api_key: apiKey })
    // Nothing typed — skip the request rather than POST an empty body that
    // reports success and changes nothing.
    if (!Object.keys(payload).length) return
    if (await write(() => setRunnerCredential(runnerId, payload), 'Failed to save')) {
      setApiKey('') // never keep a secret in component state after it lands
      setSaved(true)
    }
  }

  const saveName = (labelKey: LabelKey, name: string) =>
    write(() => setRunnerCredential(runnerId, { [labelKey]: name.trim() }), 'Failed to rename')

  const swap = () => write(() => swapRunnerLogins(runnerId), 'Failed to swap')

  // Deleting a backup token: an empty value clears its slot (the API stores
  // nothing for an empty secret) and the name goes with it. Confirmed first,
  // because a token is write-only — once removed, only a person holding it can
  // put it back.
  const remove = (key: SlotKey, labelKey: LabelKey, label: string) => {
    if (!window.confirm(`Remove ${label}? The runner stops using it within a few minutes.`)) return
    return write(() => setRunnerCredential(runnerId, { [key]: '', [labelKey]: '' }), 'Failed to remove')
  }
  const canSwap = !busy && !!status
    && (status.has_claude_token || status.has_claude_token_secondary)

  if (status === null && error === null) {
    return <div className="h-6 w-48 animate-pulse rounded-md bg-muted" data-testid="runner-credentials-loading" />
  }

  const summary = status ? credentialSummary(status, signedIn) : null
  const heldTokens = SLOTS.filter((s) => s.login && status?.[s.statusKey]).length
  const emptySlot = status ? SLOTS.find((s) => s.login && !status[s.statusKey]) : undefined
  const apiSlot = SLOTS.find((s) => !s.login)!
  const apiKeySet = status?.has_claude_api_key ?? false

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

      {summary?.warning && (
        <p className="rounded-md border border-warning/40 bg-warning/10 px-2 py-1 text-[12px] text-warning" data-testid="runner-credentials-warning">
          ⚠ {summary.warning}
        </p>
      )}

      <ClaudeLogins
        runnerId={runnerId}
        logins={status?.logins ?? []}
        signedIn={signedIn}
        signedInAs={signedInAs}
        busy={busy}
        write={write}
        reload={load}
      />

      {/* Backup tokens: `claude setup-token`, good for a year with nothing to
          renew. The full logins above renew themselves, and on 2026-10-15
          cloud-ec2-2's stopped renewing with both backups removed — so the box
          stopped. A backup token is what keeps a box running then, so the page
          can always get one, and will not remove the last one. The buttons
          say "token", never "Sign in with Claude": side by side under the same
          words, an operator once got a token believing it was the full login. */}
      <div className="flex flex-col gap-0.5 pt-1">
        <span className="text-[11px] uppercase tracking-wide text-muted-foreground">Backup tokens</span>
        <span className="text-[11px] text-foreground-subtle">
          Keep this runner going if the Claude logins above stop working. Good for a year with
          nothing to renew, but they cannot use claude.ai connectors (Claude Docs).
        </span>
      </div>
      {status && heldTokens === 0 && (
        <p className="rounded-md border border-warning/40 bg-warning/10 px-2 py-1 text-[12px] text-warning" data-testid="cred-no-backup-warning">
          ⚠ No backup token: if the Claude login stops working, this runner stops.
        </p>
      )}
      {SLOTS.filter((s) => s.login && status?.[s.statusKey]).map((slot, i) => {
        const login = slot.login!
        const isSet = true
        const isLast = heldTokens === 1
        const dropHere = dragging && dragging !== login.slot && dropTarget === login.slot
        return (
          <div key={slot.key}>
            {/* Between the two logins: the keyboard/touch way to reorder, since
                HTML drag-and-drop does nothing on a phone. */}
            {i === 1 && (
              <div className="-my-1 flex justify-center">
                <button
                  type="button"
                  onClick={() => void swap()}
                  disabled={!canSwap}
                  data-testid="runner-credentials-swap"
                  title="Make the fallback the primary"
                  className="rounded-md px-2 py-0.5 text-[11px] text-muted-foreground hover:bg-muted hover:text-foreground disabled:opacity-40"
                >
                  ⇅ swap
                </button>
              </div>
            )}
            <div
              data-testid={`cred-slot-${slot.key}`}
              onDragOver={(e) => {
                if (dragging && dragging !== login.slot) {
                  e.preventDefault()
                  setDropTarget(login.slot)
                }
              }}
              onDragLeave={() => setDropTarget(null)}
              onDrop={(e) => {
                e.preventDefault()
                const from = dragging
                setDragging(null)
                setDropTarget(null)
                if (from && from !== login.slot) void swap()
              }}
              className={`flex flex-col gap-1.5 rounded-md border p-2 ${
                dropHere ? 'border-primary bg-primary/5' : 'border-border'
              } ${dragging === login.slot ? 'opacity-50' : ''}`}
            >
              <div className="flex items-center gap-2 text-[12px] text-foreground">
                {/* Only the handle drags: a draggable row would steal the mouse
                    from its own controls. The whole row is still the drop target. */}
                <span
                  aria-hidden
                  draggable={canSwap}
                  onDragStart={(e) => {
                    e.dataTransfer.effectAllowed = 'move'
                    e.dataTransfer.setData('text/plain', login.slot)
                    const row = e.currentTarget.closest('[data-testid^="cred-slot-"]')
                    if (row instanceof HTMLElement) e.dataTransfer.setDragImage(row, 12, 12)
                    setDragging(login.slot)
                  }}
                  onDragEnd={() => { setDragging(null); setDropTarget(null) }}
                  data-testid={`cred-drag-${slot.key}`}
                  title={canSwap ? 'Drag onto the other login to swap them' : undefined}
                  className={`select-none text-foreground-subtle ${canSwap ? 'cursor-grab' : 'opacity-40'}`}
                >
                  ⋮⋮
                </span>
                <span className={isSet ? 'text-success' : 'text-muted-foreground'}>{isSet ? '●' : '○'}</span>
                <span className="font-medium">{slot.label}</span>
                <LoginName
                  slotKey={slot.key}
                  name={status?.[login.labelKey] ?? ''}
                  disabled={busy}
                  onSave={(name) => saveName(login.labelKey, name)}
                />
                {isSet && (
                  <button
                    type="button"
                    onClick={() => void remove(slot.key, login.labelKey, slot.label)}
                    disabled={busy || isLast}
                    title={isLast ? 'The last backup token — get another one before removing this one' : undefined}
                    data-testid={`cred-remove-${slot.key}`}
                    className="ml-auto rounded-md px-2 py-0.5 text-[11px] text-muted-foreground hover:bg-muted hover:text-destructive disabled:opacity-40"
                  >
                    Remove
                  </button>
                )}
              </div>
              <span className="text-[11px] text-foreground-subtle">{slot.hint}</span>
              <RunnerReauth runnerId={runnerId} slot={login.slot} isSet onSignedIn={load} />
            </div>
          </div>
        )
      })}
      {/* The next empty slot, as a way to fill it — never as a "not set" row. */}
      {emptySlot && (
        <div className="rounded-md border border-dashed border-border p-2" data-testid={`cred-get-${emptySlot.key}`}>
          <RunnerReauth runnerId={runnerId} slot={emptySlot.login!.slot} onSignedIn={load} />
        </div>
      )}

      {/* The API key is not a login: there is nothing to sign in to, so it is
          the one credential still pasted. */}
      <label className="flex flex-col gap-0.5 pt-1" data-testid={`cred-slot-${apiSlot.key}`}>
        <span className="flex items-center gap-2 text-[12px] text-foreground">
          <span className={apiKeySet ? 'text-success' : 'text-muted-foreground'}>{apiKeySet ? '●' : '○'}</span>
          {apiSlot.label}
          <span className="text-[11px] text-foreground-subtle">{apiKeySet ? 'set' : 'not set'}</span>
        </span>
        <div className="flex gap-2">
          <input
            type="password"
            autoComplete="off"
            value={apiKey}
            onChange={(e) => setApiKey(e.target.value)}
            placeholder={apiKeySet ? 'paste to replace' : 'paste to set'}
            aria-label={apiSlot.label}
            className="flex-1 rounded-md border border-input bg-input px-2 py-1 font-mono text-[12px] text-foreground placeholder:text-muted-foreground"
          />
          <button
            type="button"
            onClick={() => void saveApiKey()}
            disabled={busy || !apiKey.trim()}
            data-testid="runner-credentials-save"
            className="rounded-md bg-primary px-2.5 py-1 text-[12px] font-medium text-primary-foreground disabled:opacity-40"
          >
            {busy ? 'Saving…' : 'Save'}
          </button>
        </div>
        <span className="text-[11px] text-foreground-subtle">{apiSlot.hint}</span>
      </label>

      {saved && <span className="text-[12px] text-success" data-testid="runner-credentials-saved">Saved.</span>}
      {error && <span className="text-[12px] text-destructive" data-testid="runner-credentials-error">{error}</span>}

      {/* Say the shape of the guarantee, because a password field that renders
          nothing back looks broken otherwise. */}
      <p className="text-[11px] text-foreground-subtle">
        Tokens are write-only: encrypted at rest, and only the paired runner can read them
        back. This page can show whether one is set, never what is in it.
      </p>
    </div>
  )
}

/** Move `key` to `to` in `keys` — the new order to send. */
export function moved(keys: string[], key: string, to: number): string[] {
  const rest = keys.filter((k) => k !== key)
  const i = Math.max(0, Math.min(to, rest.length))
  return [...rest.slice(0, i), key, ...rest.slice(i)]
}

/** The box's full Claude logins — THE way to sign a runner in to Claude.
 *
 *  Each is a `claude auth login` the runner keeps on the box, in a directory
 *  of its own: the only kind of login that carries claude.ai connectors (Claude
 *  Docs and the rest), and its refresh token rotates, so it never comes back
 *  here. As many as a person adds, tried in the order shown — when one hits its
 *  usage cap the runner moves to the next. The box reports which account each
 *  one is, so nobody types a name that can disagree with the login it holds. */
function ClaudeLogins({ runnerId, logins, signedIn, signedInAs, busy, write, reload }: {
  runnerId: string
  logins: readonly RunnerLogin[]
  signedIn: boolean | null
  signedInAs: string
  busy: boolean
  write: (fn: () => Promise<CredentialStatus>, fallback: string) => Promise<boolean>
  reload: () => Promise<void>
}) {
  const [dragging, setDragging] = useState<string | null>(null)
  const [dropTarget, setDropTarget] = useState<string | null>(null)
  const keys = logins.map((l) => l.key)

  const add = () => write(() => addRunnerLogin(runnerId), 'Failed to add a login')
  const remove = (login: RunnerLogin, n: number) => {
    const who = login.account || `login ${n}`
    if (!window.confirm(`Remove ${who}? The runner signs it out within a few minutes.`)) return
    return write(() => removeRunnerLogin(runnerId, login.key), 'Failed to remove')
  }
  const reorder = (key: string, to: number) => {
    const next = moved(keys, key, to)
    if (next.join() === keys.join()) return
    return write(() => orderRunnerLogins(runnerId, next), 'Failed to reorder')
  }

  return (
    <div className="flex flex-col gap-1.5" data-testid="cred-logins">
      <div className="flex items-baseline gap-2">
        <span className="text-[12px] font-medium text-foreground">Claude logins</span>
        <span className="text-[11px] text-foreground-subtle">
          tried in this order — when one hits its usage cap the runner moves to the next
        </span>
      </div>

      {/* A box signed in before there was a list: the runner adds it here on
          its next re-read. Until then, say what the box says. */}
      {logins.length === 0 && signedIn && (
        <div className="flex items-center gap-2 rounded-md border border-border p-2 text-[12px]" data-testid="cred-login-pending-adopt">
          <span className="text-success">●</span>
          <span className="text-foreground">{signedInAs || 'signed in on this runner'}</span>
          <span className="text-[11px] text-foreground-subtle">joins this list when the runner next checks in</span>
        </div>
      )}

      {logins.map((login, i) => {
        const dropHere = dragging && dragging !== login.key && dropTarget === login.key
        return (
          <div
            key={login.key}
            data-testid={`cred-login-${login.key}`}
            onDragOver={(e) => {
              if (dragging && dragging !== login.key) {
                e.preventDefault()
                setDropTarget(login.key)
              }
            }}
            onDragLeave={() => setDropTarget(null)}
            onDrop={(e) => {
              e.preventDefault()
              const from = dragging
              setDragging(null)
              setDropTarget(null)
              if (from && from !== login.key) void reorder(from, i)
            }}
            className={`flex flex-col gap-1.5 rounded-md border p-2 ${
              dropHere ? 'border-primary bg-primary/5' : 'border-border'
            } ${dragging === login.key ? 'opacity-50' : ''}`}
          >
            <div className="flex items-center gap-2 text-[12px] text-foreground">
              {/* Only the handle drags: a draggable row would steal the mouse
                  from its own controls. The whole row is the drop target. */}
              <span
                aria-hidden
                draggable={!busy && logins.length > 1}
                onDragStart={(e) => {
                  e.dataTransfer.effectAllowed = 'move'
                  e.dataTransfer.setData('text/plain', login.key)
                  const row = e.currentTarget.closest('[data-testid^="cred-login-"]')
                  if (row instanceof HTMLElement) e.dataTransfer.setDragImage(row, 12, 12)
                  setDragging(login.key)
                }}
                onDragEnd={() => { setDragging(null); setDropTarget(null) }}
                title={logins.length > 1 ? 'Drag to reorder' : undefined}
                className={`select-none text-foreground-subtle ${logins.length > 1 ? 'cursor-grab' : 'opacity-40'}`}
              >
                ⋮⋮
              </span>
              <span className="w-4 text-right tabular-nums text-foreground-subtle">{i + 1}</span>
              <span className={login.signed_in ? 'text-success' : 'text-muted-foreground'}>
                {login.signed_in ? '●' : '○'}
              </span>
              <span className="truncate font-medium" data-testid={`cred-login-account-${login.key}`}>
                {login.signed_in
                  ? (login.account || 'signed in on this runner')
                  : (login.account ? `${login.account} — signed out` : 'not signed in yet')}
              </span>
              <span className="ml-auto flex items-center gap-0.5">
                {/* Buttons as well as dragging: HTML drag-and-drop does nothing
                    on a phone. */}
                <button
                  type="button"
                  onClick={() => void reorder(login.key, i - 1)}
                  disabled={busy || i === 0}
                  data-testid={`cred-login-up-${login.key}`}
                  aria-label="Move up"
                  className="rounded-md px-1.5 py-0.5 text-[11px] text-muted-foreground hover:bg-muted hover:text-foreground disabled:opacity-30"
                >
                  ↑
                </button>
                <button
                  type="button"
                  onClick={() => void reorder(login.key, i + 1)}
                  disabled={busy || i === logins.length - 1}
                  data-testid={`cred-login-down-${login.key}`}
                  aria-label="Move down"
                  className="rounded-md px-1.5 py-0.5 text-[11px] text-muted-foreground hover:bg-muted hover:text-foreground disabled:opacity-30"
                >
                  ↓
                </button>
                <button
                  type="button"
                  onClick={() => void remove(login, i + 1)}
                  disabled={busy}
                  data-testid={`cred-login-remove-${login.key}`}
                  className="rounded-md px-2 py-0.5 text-[11px] text-muted-foreground hover:bg-muted hover:text-destructive disabled:opacity-40"
                >
                  Remove
                </button>
              </span>
            </div>
            <RunnerReauth
              runnerId={runnerId}
              slot="box"
              loginKey={login.key === 'default' ? '' : login.key}
              isSet={login.signed_in}
              onSignedIn={reload}
            />
          </div>
        )
      })}

      <button
        type="button"
        onClick={() => void add()}
        disabled={busy}
        data-testid="cred-login-add"
        className="w-fit rounded-md border border-dashed border-border px-2.5 py-1 text-[12px] text-muted-foreground hover:bg-muted hover:text-foreground disabled:opacity-40"
      >
        + Add a login
      </button>
      <span className="text-[11px] text-foreground-subtle">
        Sign each in as an account whose Claude the agents may use, including its claude.ai
        connectors (Claude Docs). Logins stay on the runner and are never stored here.
      </span>
    </div>
  )
}

/** A login's name, edited in place. The token cannot say whose it is (Claude
 *  refuses a setup-token its own profile), so a person names it, once. */
function LoginName({ slotKey, name, disabled, onSave }: {
  slotKey: SlotKey
  name: string
  disabled: boolean
  onSave: (name: string) => Promise<boolean>
}) {
  const [editing, setEditing] = useState(false)
  const [value, setValue] = useState(name)

  const commit = async () => {
    if (value.trim() === name) { setEditing(false); return }
    if (await onSave(value)) setEditing(false)
  }

  if (!editing) {
    return (
      <button
        type="button"
        onClick={() => { setValue(name); setEditing(true) }}
        data-testid={`cred-name-${slotKey}`}
        title="Rename"
        className={`truncate rounded px-1 text-left hover:bg-muted ${
          name ? 'text-foreground-secondary' : 'text-[11px] italic text-foreground-subtle'
        }`}
      >
        {name || 'name this login'} <span className="text-foreground-subtle">✎</span>
      </button>
    )
  }
  return (
    <input
      autoFocus
      value={value}
      disabled={disabled}
      onChange={(e) => setValue(e.target.value)}
      onBlur={() => void commit()}
      onKeyDown={(e) => {
        if (e.key === 'Enter') void commit()
        if (e.key === 'Escape') setEditing(false)
      }}
      placeholder="e.g. you@dimagi.com"
      maxLength={200}
      data-testid={`cred-label-${slotKey}`}
      className="min-w-0 flex-1 rounded-md border border-input bg-input px-2 py-0.5 text-[12px] text-foreground placeholder:text-muted-foreground"
    />
  )
}
