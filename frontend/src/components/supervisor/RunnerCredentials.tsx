import { useCallback, useEffect, useRef, useState, type JSX } from 'react'
import {
  getRunnerCredentialStatus,
  setRunnerCredential,
  swapRunnerLogins,
  type CredentialStatus,
  type LoginSlot,
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
    label: 'Primary login',
    hint: 'Used first. When it hits its cap the runner moves to the fallback, and the two swap places, so the one that works is always on top.',
    login: { slot: 'primary', labelKey: 'claude_token_label' },
  },
  {
    key: 'claude_token_secondary',
    statusKey: 'has_claude_token_secondary',
    label: 'Fallback login',
    hint: 'A second subscription. Without one, a weekly cap on the primary stops every agent on this box.',
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

export function credentialSummary(s: CredentialStatus): Summary {
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
  const warning: string | null = s.has_claude_token
    ? null
    : 'No Claude credential at all — this runner cannot execute any turn.'
  return { claudeFallback: fallback, warning, unset }
}

export function RunnerCredentials({ runnerId }: { runnerId: string }): JSX.Element {
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
  const canSwap = !busy && !!status
    && (status.has_claude_token || status.has_claude_token_secondary)

  if (status === null && error === null) {
    return <div className="h-6 w-48 animate-pulse rounded-md bg-muted" data-testid="runner-credentials-loading" />
  }

  const summary = status ? credentialSummary(status) : null
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

      {SLOTS.filter((s) => s.login).map((slot, i) => {
        const login = slot.login!
        const isSet = status?.[slot.statusKey] ?? false
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
                {!isSet && <span className="text-[11px] text-foreground-subtle">not set</span>}
              </div>
              <RunnerReauth runnerId={runnerId} slot={login.slot} isSet={isSet} onSignedIn={load} />
              <span className="text-[11px] text-foreground-subtle">{slot.hint}</span>
            </div>
          </div>
        )
      })}

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
