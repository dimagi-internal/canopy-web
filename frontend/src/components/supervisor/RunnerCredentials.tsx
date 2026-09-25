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
    hint: 'Used first. Or paste a dedicated `claude setup-token` — long-lived and non-rotating, never a live OAuth blob whose refresh token rotates.',
    login: { slot: 'primary', labelKey: 'claude_token_label' },
  },
  {
    key: 'claude_token_secondary',
    statusKey: 'has_claude_token_secondary',
    label: 'Fallback login',
    hint: 'A SECOND subscription, used when the primary hits its cap. Without one, a weekly cap stops every agent on this box.',
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

/** Names that actually changed. Unlike a token, an emptied name is a real edit
 *  ("clear it"), so "" is sent — but an untouched one is not sent at all. */
export function labelPayload(
  draft: Partial<Record<LabelKey, string>>,
  status: CredentialStatus | null,
): Partial<Record<LabelKey, string>> {
  const out: Partial<Record<LabelKey, string>> = {}
  for (const [k, v] of Object.entries(draft) as [LabelKey, string | undefined][]) {
    if (v === undefined) continue
    const trimmed = v.trim()
    if (trimmed !== (status?.[k] ?? '')) out[k] = trimmed
  }
  return out
}

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
  const [draft, setDraft] = useState<Partial<Record<SlotKey, string>>>({})
  const [names, setNames] = useState<Partial<Record<LabelKey, string>>>({})
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

  const pending = { ...nextPayload(draft), ...labelPayload(names, status) }
  const dirty = Object.keys(pending).length > 0

  const save = async () => {
    // Nothing typed — skip the request rather than POST an empty body that
    // reports success and changes nothing.
    if (!dirty) return
    setBusy(true)
    setError(null)
    setSaved(false)
    try {
      const s = await setRunnerCredential(runnerId, pending)
      if (!alive.current) return
      setStatus(s)
      setDraft({}) // never keep a secret in component state after it lands
      setNames({})
      setSaved(true)
    } catch (e) {
      if (alive.current) setError(e instanceof Error ? e.message : 'Failed to save')
    } finally {
      if (alive.current) setBusy(false)
    }
  }

  // Reordering moves what is SAVED. Refused while anything is typed, because an
  // unsaved entry is keyed by position and would silently land on the other login.
  const swap = async () => {
    if (dirty) return
    setBusy(true)
    setError(null)
    setSaved(false)
    try {
      const s = await swapRunnerLogins(runnerId)
      if (alive.current) setStatus(s)
    } catch (e) {
      if (alive.current) setError(e instanceof Error ? e.message : 'Failed to swap')
    } finally {
      if (alive.current) setBusy(false)
    }
  }
  const canSwap = !busy && !dirty && !!status
    && (status.has_claude_token || status.has_claude_token_secondary)

  if (status === null && error === null) {
    return <div className="h-6 w-48 animate-pulse rounded-md bg-muted" data-testid="runner-credentials-loading" />
  }

  const summary = status ? credentialSummary(status) : null

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

      {SLOTS.map((slot, i) => {
        const isSet = status?.[slot.statusKey] ?? false
        const login = slot.login
        const name = login ? (names[login.labelKey] ?? status?.[login.labelKey] ?? '') : ''
        const savedName = login ? status?.[login.labelKey] ?? '' : ''
        const dropHere = login && dragging && dragging !== login.slot && dropTarget === login.slot
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
                  title={dirty ? 'Save or clear what you typed first' : 'Make the fallback the primary'}
                  className="rounded-md px-2 py-0.5 text-[11px] text-muted-foreground hover:bg-muted hover:text-foreground disabled:opacity-40"
                >
                  ⇅ swap primary and fallback
                </button>
              </div>
            )}
            <div
              data-testid={`cred-slot-${slot.key}`}
              onDragOver={login ? (e) => {
                if (dragging && dragging !== login.slot) {
                  e.preventDefault()
                  setDropTarget(login.slot)
                }
              } : undefined}
              onDragLeave={() => setDropTarget(null)}
              onDrop={login ? (e) => {
                e.preventDefault()
                const from = dragging
                setDragging(null)
                setDropTarget(null)
                if (from && from !== login.slot) void swap()
              } : undefined}
              className={`flex flex-col gap-1 rounded-md border p-2 ${
                dropHere ? 'border-primary bg-primary/5' : 'border-border'
              } ${dragging === login?.slot ? 'opacity-50' : ''}`}
            >
              <span className="flex items-center gap-2 text-[12px] text-foreground">
                {/* Only the handle drags: a draggable row would steal the mouse
                    from its own inputs. The whole row is still the drop target. */}
                {login && (
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
                )}
                <span className={isSet ? 'text-success' : 'text-muted-foreground'}>{isSet ? '●' : '○'}</span>
                <span className="font-medium">{slot.label}</span>
                {savedName && (
                  <span className="truncate text-foreground-secondary" data-testid={`cred-name-${slot.key}`}>
                    {savedName}
                  </span>
                )}
                <span className="text-[11px] text-foreground-subtle">{isSet ? 'set' : 'not set'}</span>
              </span>

              {login && (
                <RunnerReauth runnerId={runnerId} slot={login.slot} isSet={isSet} onSignedIn={load} />
              )}

              {login && (
                <input
                  type="text"
                  autoComplete="off"
                  value={name}
                  onChange={(e) => setNames((d) => ({ ...d, [login.labelKey]: e.target.value }))}
                  placeholder="whose account? e.g. you@dimagi.com"
                  aria-label={`${slot.label} name`}
                  data-testid={`cred-label-${slot.key}`}
                  className="rounded-md border border-input bg-input px-2 py-1 text-[12px] text-foreground placeholder:text-muted-foreground"
                />
              )}
              <input
                type="password"
                autoComplete="off"
                value={draft[slot.key] ?? ''}
                onChange={(e) => setDraft((d) => ({ ...d, [slot.key]: e.target.value }))}
                placeholder={login
                  ? (isSet ? 'or paste a token to replace it' : 'or paste a token')
                  : (isSet ? 'leave blank to keep current' : 'paste to set')}
                aria-label={slot.label}
                className="rounded-md border border-input bg-input px-2 py-1 font-mono text-[12px] text-foreground placeholder:text-muted-foreground"
              />
              <span className="text-[11px] text-foreground-subtle">{slot.hint}</span>
            </div>
          </div>
        )
      })}

      <div className="flex items-center gap-2">
        <button
          type="button"
          onClick={() => void save()}
          disabled={busy || !dirty}
          data-testid="runner-credentials-save"
          className="rounded-md bg-primary px-2.5 py-1 text-[12px] font-medium text-primary-foreground disabled:opacity-40"
        >
          {busy ? 'Saving…' : 'Save'}
        </button>
        {saved && <span className="text-[12px] text-success" data-testid="runner-credentials-saved">Saved.</span>}
        {error && <span className="text-[12px] text-destructive" data-testid="runner-credentials-error">{error}</span>}
      </div>

      {/* Say the shape of the guarantee, because a password field that renders
          nothing back looks broken otherwise. */}
      <p className="text-[11px] text-foreground-subtle">
        Values are write-only: they are encrypted at rest and only the paired runner can read them
        back. This page can show whether a slot is set, never what is in it.
      </p>
    </div>
  )
}
