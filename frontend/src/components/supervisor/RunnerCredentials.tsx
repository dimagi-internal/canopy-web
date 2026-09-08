import { useCallback, useEffect, useRef, useState, type JSX } from 'react'
import {
  getRunnerCredentialStatus,
  setRunnerCredential,
  type CredentialStatus,
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
  | 'github_token'
  | 'op_sa_token'

export interface Slot {
  key: SlotKey
  statusKey: `has_${SlotKey}`
  label: string
  hint: string
}

// Claude auth is an ORDERED CASCADE, not one credential — a subscription has a
// weekly cap, and when it trips every agent on the box stops. Order here is the
// order the runner falls through them.
export const SLOTS: readonly Slot[] = [
  {
    key: 'claude_token',
    statusKey: 'has_claude_token',
    label: 'Claude login (primary)',
    hint: 'A dedicated `claude setup-token`. Long-lived and non-rotating — do not paste a live OAuth blob whose refresh token rotates.',
  },
  {
    key: 'claude_token_secondary',
    statusKey: 'has_claude_token_secondary',
    label: 'Claude login (fallback)',
    hint: 'A SECOND subscription. Without one, a weekly cap on the primary stops every agent on this box.',
  },
  {
    key: 'claude_api_key',
    statusKey: 'has_claude_api_key',
    label: 'Claude API key (last resort)',
    hint: 'Metered, deliberately — falling back this far should notify a human rather than quietly spend money.',
  },
  {
    key: 'github_token',
    statusKey: 'has_github_token',
    label: 'GitHub token',
    hint: 'Read-only; clones the private agent repos at bootstrap.',
  },
  {
    key: 'op_sa_token',
    statusKey: 'has_op_sa_token',
    label: '1Password service account',
    hint: "Resolves each agent's secrets. Without it, bootstrap skips provisioning and the box comes up unable to act as any agent.",
  },
]

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

  const save = async () => {
    const payload = nextPayload(draft)
    // Nothing typed — skip the request rather than POST an empty body that
    // reports success and changes nothing.
    if (!Object.keys(payload).length) return
    setBusy(true)
    setError(null)
    setSaved(false)
    try {
      const s = await setRunnerCredential(runnerId, payload)
      if (!alive.current) return
      setStatus(s)
      setDraft({}) // never keep a secret in component state after it lands
      setSaved(true)
    } catch (e) {
      if (alive.current) setError(e instanceof Error ? e.message : 'Failed to save')
    } finally {
      if (alive.current) setBusy(false)
    }
  }

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

      {/* Above the paste-a-token fields on purpose: getting the token used to be
          the hard part (a terminal on the box), and pasting one was the easy
          part that already had a form. This is the hard part, now a link and a
          code box. The fields below stay for a token minted elsewhere. */}
      <RunnerReauth runnerId={runnerId} onSignedIn={load} />

      {SLOTS.map((slot) => {
        const isSet = status?.[slot.statusKey] ?? false
        return (
          <label key={slot.key} className="flex flex-col gap-0.5" data-testid={`cred-slot-${slot.key}`}>
            <span className="flex items-center gap-2 text-[12px] text-foreground">
              <span className={isSet ? 'text-success' : 'text-muted-foreground'}>{isSet ? '●' : '○'}</span>
              {slot.label}
              <span className="text-[11px] text-foreground-subtle">{isSet ? 'set' : 'not set'}</span>
            </span>
            <input
              type="password"
              autoComplete="off"
              value={draft[slot.key] ?? ''}
              onChange={(e) => setDraft((d) => ({ ...d, [slot.key]: e.target.value }))}
              placeholder={isSet ? 'leave blank to keep current' : 'paste to set'}
              aria-label={slot.label}
              className="rounded-md border border-input bg-input px-2 py-1 font-mono text-[12px] text-foreground placeholder:text-muted-foreground"
            />
            <span className="text-[11px] text-foreground-subtle">{slot.hint}</span>
          </label>
        )
      })}

      <div className="flex items-center gap-2">
        <button
          type="button"
          onClick={() => void save()}
          disabled={busy || !Object.keys(nextPayload(draft)).length}
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
