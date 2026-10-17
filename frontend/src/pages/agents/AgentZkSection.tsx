import { useCallback, useEffect, useRef, useState } from 'react'
import { getKeyset, getZkState, type ZkStateOut } from '@/api/vault'
import { grantPending, recoverWithCode, rotateVaultKey, turnOffZk, turnOnZk, unlockVaultKey, type Unlock } from '@/vault/operations'
import { PrfUnsupportedError, PRF_UNSUPPORTED } from '@/vault/prf'
import { useUnlockPrompt } from '@/vault/UnlockPrompt'

// Zero-knowledge for this agent (Canopy Vault Phase 2, canopy-web#1550). With
// it on, values are encrypted in a member's browser (or by the canopy CLI on a
// box) under the agent's vault key, and canopy-web stores only ciphertext and
// that key wrapped to each device and person. Every flow lives in
// `@/vault/operations`; this section is its controls and its one-time
// recovery-code dialog.

/** The trust assumption that remains, stated where the switch is. */
export const ZK_TRUST_CAVEAT =
  'One trust assumption remains: this page runs JavaScript served by canopy-web. A compromised canopy-web could ' +
  'serve a page that captures the vault key when you unlock it here. The canopy CLI on your own machine does not ' +
  'depend on that.'

function message(e: unknown, fallback: string): string {
  if (e instanceof PrfUnsupportedError) return PRF_UNSUPPORTED
  if (e instanceof Error && e.name === 'NotAllowedError') return 'The passkey prompt was dismissed.'
  return e instanceof Error ? e.message : fallback
}

function download(slug: string, code: string, kv: number) {
  const text = `Canopy vault recovery code\nAgent: ${slug}\nVault key version: ${kv}\n\n${code}\n\n` +
    'Anyone with this code and access to the agent can read its secrets. Keep it offline.\n'
  const url = URL.createObjectURL(new Blob([text], { type: 'text/plain' }))
  const a = document.createElement('a')
  a.href = url
  a.download = `canopy-vault-recovery-${slug}-kv${kv}.txt`
  a.click()
  URL.revokeObjectURL(url)
}

export function AgentZkSection({
  slug,
  storedNames,
  canEdit,
  onState,
  onChanged,
  unlock: unlockProp,
  unlockElement,
}: {
  slug: string
  /** Credentials stored in canopy-web right now (what "turn on" encrypts). */
  storedNames: string[]
  canEdit: boolean
  /** Reports the state up, so Save/Reveal on the rows can go zero-knowledge. */
  onState?: (s: ZkStateOut | null) => void
  /** The credential rows changed (enable, rotate, turn off). */
  onChanged?: () => void
  /** The panel's shared unlock (one prompt for the whole page), if any. */
  unlock?: Unlock
  unlockElement?: React.ReactNode
}) {
  const own = useUnlockPrompt()
  const unlock: Unlock = unlockProp ?? (() => unlockVaultKey(own.prompt))
  const [state, setState] = useState<ZkStateOut | null>(null)
  const [hasKey, setHasKey] = useState<boolean | null>(null)
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [code, setCode] = useState<{ code: string; kv: number } | null>(null)
  const [saved, setSaved] = useState(false)
  const [recovering, setRecovering] = useState(false)
  const [typed, setTyped] = useState('')
  const pendingConfirm = useRef<{ resolve: () => void; reject: (e: Error) => void } | null>(null)

  const update = useCallback((s: ZkStateOut | null) => {
    setState(s)
    onState?.(s)
  }, [onState])

  useEffect(() => {
    let cancelled = false
    if (!canEdit) return
    getZkState(slug)
      .then((s) => !cancelled && update(s))
      .catch(() => !cancelled && update(null))
    getKeyset()
      .then((k) => !cancelled && setHasKey(k.exists))
      .catch(() => !cancelled && setHasKey(false))
    return () => {
      cancelled = true
    }
  }, [slug, canEdit, update])

  if (!canEdit || state === null) return null

  /** Show the recovery code ONCE; resolves when the person says they kept it. */
  const confirmCode = (kv: number) => (c: string) =>
    new Promise<void>((resolve, reject) => {
      pendingConfirm.current = { resolve, reject }
      setSaved(false)
      setCode({ code: c, kv })
    })

  const closeCode = (ok: boolean) => {
    const p = pendingConfirm.current
    pendingConfirm.current = null
    setCode(null)
    if (ok) p?.resolve()
    else p?.reject(new Error('Cancelled — nothing was changed.'))
  }

  const run = async (label: string, fn: () => Promise<ZkStateOut>, changed = false) => {
    setBusy(label)
    setError(null)
    try {
      update(await fn())
      if (changed) onChanged?.()
    } catch (e) {
      setError(message(e, `Could not ${label}`))
    } finally {
      setBusy(null)
    }
  }

  const pending = state.pending
  const current = state.wraps.filter((w) => w.kv === state.kv)
  return (
    <section id="cred-zk" className="mb-6" data-testid="agent-zk">
      <h3 className="mb-2 text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
        Zero-knowledge
      </h3>

      {!state.enabled ? (
        <div className="space-y-2 text-[12px]">
          <p className="text-muted-foreground">
            Off: canopy-web encrypts these values at rest with a key it holds. On: they are encrypted in your browser
            under a key only this agent&rsquo;s devices and admins hold, and canopy-web stores ciphertext it cannot
            read.
          </p>
          {hasKey === false ? (
            <p className="text-warning" data-testid="zk-needs-vault-key">
              Set up your vault key first: <a className="underline" href="/settings#vault-key">Settings → Vault key</a>.
              Agents you create after that start zero-knowledge.
            </p>
          ) : (
            <>
              <p className="text-muted-foreground">
                Turning it on reads each stored value once (your passkey), encrypts it here, and wraps the new vault
                key for you, {pending.filter((p) => p.kind === 'device').length} approved device(s) with an ECDH key,
                and a recovery code you keep.
              </p>
              <button
                type="button"
                disabled={busy !== null}
                onClick={() => void run('turn on zero-knowledge', () => turnOnZk(slug, storedNames, confirmCode(1)), true)}
                className="min-h-11 rounded-md bg-primary px-3 py-1 text-[12px] font-medium text-primary-foreground disabled:opacity-40 sm:min-h-0"
                data-testid="zk-enable"
              >
                {busy ? 'Working…' : 'Turn on zero-knowledge'}
              </button>
            </>
          )}
        </div>
      ) : state.kv === 0 ? (
        // Born zero-knowledge (canopy-web#1550): no vault key yet. Every value
        // that arrives is sealed to the owner and the agent's devices.
        <div className="space-y-2 text-[12px]" data-testid="zk-born">
          <p className="text-foreground">
            Zero-knowledge from the start · no vault key yet. Values saved now are sealed to you and this agent&rsquo;s
            approved devices{(state.sealed ?? []).length ? ` (${(state.sealed ?? []).length} so far)` : ''}; canopy-web
            cannot read them.
          </p>
          <p className="text-muted-foreground">
            Create the vault key to finish: it re-encrypts them under one key, wraps it for you and each device, and
            gives you a recovery code.
          </p>
          <button
            type="button"
            disabled={busy !== null}
            onClick={() => void run('create the vault key',
              () => turnOnZk(slug, storedNames, confirmCode(1), unlock), true)}
            className="min-h-11 rounded-md bg-primary px-3 py-1 text-[12px] font-medium text-primary-foreground disabled:opacity-40 sm:min-h-0"
            data-testid="zk-create-key"
          >
            {busy ? 'Working…' : 'Create the vault key'}
          </button>
        </div>
      ) : (
        <div className="space-y-2 text-[12px]">
          <p className="text-foreground" data-testid="zk-on">
            On · vault key version {state.kv}. canopy-web holds only ciphertext for this agent.
          </p>
          <table className="w-full text-[12px]" data-testid="zk-wraps">
            <thead>
              <tr className="text-left text-muted-foreground">
                <th className="py-1 font-medium">Holds the vault key</th>
                <th className="py-1 font-medium">As</th>
                <th className="py-1 font-medium" />
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {current.map((w) => (
                <tr key={`${w.kind}:${w.target ?? ''}`}>
                  <td className="py-1.5 text-foreground">{w.kind === 'recovery' ? 'Recovery code' : w.label}</td>
                  <td className="py-1.5 text-muted-foreground">{w.kind}</td>
                  <td className="py-1.5 text-right">
                    {!w.current && <span className="text-warning">no longer qualifies — rotate</span>}
                  </td>
                </tr>
              ))}
              {pending.map((p) => (
                <tr key={`pending:${p.kind}:${p.target}`} data-testid={`zk-pending-${p.target}`}>
                  <td className="py-1.5 text-foreground">
                    {p.kind === 'device' ? `${p.runner_name} · ${p.label || p.device_kind}` : p.email}
                  </td>
                  <td className="py-1.5 text-muted-foreground">{p.kind}</td>
                  <td className="py-1.5 text-right text-warning">pending</td>
                </tr>
              ))}
            </tbody>
          </table>
          {(state.sealed ?? []).length > 0 && (
            <p className="text-muted-foreground" data-testid="zk-sealed">
              canopy-web sealed {(state.sealed ?? []).join(', ')} to this agent&rsquo;s devices and admins when it made
              {(state.sealed ?? []).length === 1 ? ' it' : ' them'}. Granting re-encrypts{' '}
              {(state.sealed ?? []).length === 1 ? 'it' : 'them'} under the vault key, so later devices can read
              {(state.sealed ?? []).length === 1 ? ' it' : ' them'} too.
            </p>
          )}
          {state.rotation_recommended && (
            <p className="text-warning" data-testid="zk-rotate-recommended">
              A device or person who no longer qualifies still holds this vault key. Rotate to retire it.
            </p>
          )}
          <div className="flex flex-wrap gap-2">
            {(pending.length > 0 || (state.sealed ?? []).length > 0) && (
              <button type="button" disabled={busy !== null} data-testid="zk-grant"
                      onClick={() => void run('grant access', () => grantPending(slug, unlock))}
                      className="min-h-11 rounded-md bg-primary px-3 py-1 text-[12px] font-medium text-primary-foreground disabled:opacity-40 sm:min-h-0">
                {pending.length > 0 ? `Grant ${pending.length} pending` : 'Re-encrypt sealed values'}
              </button>
            )}
            <button type="button" disabled={busy !== null} data-testid="zk-rotate"
                    onClick={() => void run('rotate the vault key',
                      () => rotateVaultKey(slug, confirmCode(state.kv + 1), unlock), true)}
                    className="min-h-11 rounded-md border border-input px-3 py-1 text-[12px] text-foreground hover:border-primary disabled:opacity-40 sm:min-h-0">
              Rotate vault key
            </button>
            <button type="button" disabled={busy !== null} data-testid="zk-recover"
                    onClick={() => setRecovering((v) => !v)}
                    className="min-h-11 rounded-md border border-input px-3 py-1 text-[12px] text-foreground hover:border-primary disabled:opacity-40 sm:min-h-0">
              Recover with code
            </button>
            <button type="button" disabled={busy !== null} data-testid="zk-disable"
                    onClick={() => {
                      if (window.confirm('Turn zero-knowledge off? Every value is decrypted here and handed back to canopy-web to hold.')) {
                        void run('turn off zero-knowledge', () => turnOffZk(slug, unlock), true)
                      }
                    }}
                    className="min-h-11 rounded-md px-3 py-1 text-[12px] text-destructive hover:bg-destructive/10 disabled:opacity-40 sm:min-h-0">
              Turn off
            </button>
          </div>
          {recovering && (
            <form className="flex flex-wrap items-center gap-2" data-testid="zk-recover-form"
                  onSubmit={(e) => {
                    e.preventDefault()
                    void run('recover', () => recoverWithCode(slug, typed)).then(() => {
                      setTyped('')
                      setRecovering(false)
                    })
                  }}>
              <input value={typed} onChange={(e) => setTyped(e.target.value)} autoComplete="off" spellCheck={false}
                     placeholder="XXXX-XXXX-…" aria-label="Recovery code"
                     className="min-h-11 w-full rounded-md border border-input bg-input px-2 py-1 font-mono text-[12px] text-foreground sm:min-h-0 sm:w-96" />
              <button type="submit" disabled={busy !== null || !typed.trim()}
                      className="min-h-11 rounded-md bg-primary px-3 py-1 text-[12px] font-medium text-primary-foreground disabled:opacity-40 sm:min-h-0">
                Wrap it to me
              </button>
            </form>
          )}
        </div>
      )}

      {code && (
        <div role="dialog" aria-modal="true" aria-label="Recovery code"
             className="mt-3 space-y-2 rounded-lg border border-warning/40 bg-warning/10 p-3 text-[12px]"
             data-testid="zk-recovery-code">
          <p className="font-medium text-foreground">Recovery code — shown once</p>
          <p className="text-muted-foreground">
            It opens this agent&rsquo;s vault key if every device and admin loses theirs. canopy-web does not keep a
            copy. Download it or write it down before you continue.
          </p>
          <code className="block select-all break-all rounded border border-border bg-card px-2 py-1 font-mono text-[13px] text-foreground">
            {code.code}
          </code>
          <div className="flex flex-wrap items-center gap-2">
            <button type="button" onClick={() => download(slug, code.code, code.kv)}
                    className="min-h-11 rounded-md border border-input px-3 py-1 text-foreground hover:border-primary sm:min-h-0">
              Download
            </button>
            <label className="flex items-center gap-1 text-foreground">
              <input type="checkbox" checked={saved} onChange={(e) => setSaved(e.target.checked)}
                     data-testid="zk-code-saved" />
              I have saved it
            </label>
            <button type="button" disabled={!saved} onClick={() => closeCode(true)} data-testid="zk-code-continue"
                    className="min-h-11 rounded-md bg-primary px-3 py-1 font-medium text-primary-foreground disabled:opacity-40 sm:min-h-0">
              Continue
            </button>
            <button type="button" onClick={() => closeCode(false)}
                    className="min-h-11 rounded-md px-3 py-1 text-muted-foreground hover:text-foreground sm:min-h-0">
              Cancel
            </button>
          </div>
        </div>
      )}

      {unlockProp ? unlockElement : own.element}
      {error && <p className="mt-2 text-[12px] text-destructive" data-testid="zk-error">{error}</p>}
      <p className="mt-2 text-[11px] text-muted-foreground" data-testid="zk-trust-caveat">{ZK_TRUST_CAVEAT}</p>
    </section>
  )
}
