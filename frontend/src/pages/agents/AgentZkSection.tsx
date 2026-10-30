import { useCallback, useEffect, useRef, useState } from 'react'
import { getZkState, type ZkStateOut } from '@/api/vault'
import {
  giveAccess,
  recoverWithCode,
  rotateVaultKey,
  unlockVaultKey,
  type Unlock,
} from '@/vault/operations'
import { LoadFailed } from '@/components/vault/LoadFailed'
import { RecoveryCodeBox } from '@/components/vault/RecoveryCodeBox'
import { keyFlowError } from '@/vault/prf'
import { useUnlockPrompt } from '@/vault/UnlockPrompt'

// Zero-knowledge for this agent (Canopy Vault Phase 2, canopy-web#1550). With
// it on, values are encrypted in a member's browser (or by the canopy CLI on a
// box) under the agent's vault key, and canopy-web stores only ciphertext and
// that key wrapped to each device and person. Every flow lives in
// `@/vault/operations`; this section is its controls and its one-time
// recovery-code dialog.

const KIND_LABEL: Record<string, string> = { device: 'machine', member: 'person', recovery: 'recovery code' }

/** The trust assumption that remains, stated where the controls are. */
export const ZK_TRUST_CAVEAT =
  'One trust assumption remains: this page runs JavaScript served by canopy-web. A compromised canopy-web could ' +
  'serve a page that captures your key when you unlock it here. The canopy CLI on your own machine does not ' +
  'depend on that.'

function recoveryFile(slug: string, code: string, kv: number, person: boolean): string {
  return person
    ? `Canopy recovery code\n\n${code}\n\nIf you lose your passkey and passphrase, this gets you back into every agent you hold. Keep it offline.\n`
    : `Canopy recovery code\nAgent: ${slug}\nKey version: ${kv}\n\n${code}\n\n` +
      'Anyone with this code and access to the agent can read its secrets. Keep it offline.\n'
}

export function AgentZkSection({
  slug,
  canEdit,
  onState,
  onChanged,
}: {
  slug: string
  canEdit: boolean
  /** Reports the state up, so Save/Reveal on the rows can go zero-knowledge. */
  onState?: (s: ZkStateOut | null) => void
  /** The credential rows changed (enable, rotate, turn off). */
  onChanged?: () => void
}) {
  const own = useUnlockPrompt()
  const unlock: Unlock = () => unlockVaultKey(own.prompt)
  const [state, setState] = useState<ZkStateOut | null>(null)
  const [busy, setBusy] = useState<string | null>(null)
  const [failed, setFailed] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [code, setCode] = useState<{ code: string; kv: number; person: boolean } | null>(null)
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
      .then((s) => { if (!cancelled) { setFailed(false); update(s) } })
      .catch(() => { if (!cancelled) { setFailed(true); update(null) } })
    return () => {
      cancelled = true
    }
  }, [slug, canEdit, update])

  if (!canEdit) return null
  if (state === null) return failed ? <LoadFailed what="its key and recovery" /> : null

  /** Show the recovery code ONCE; resolves when the person says they kept it. */
  const confirmCode = (kv: number, person = false) => (c: string) =>
    new Promise<void>((resolve, reject) => {
      pendingConfirm.current = { resolve, reject }
      setCode({ code: c, kv, person })
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
      setError(keyFlowError(e, `Could not ${label}`))
    } finally {
      setBusy(null)
    }
  }

  const pending = state.pending
  const current = state.wraps.filter((w) => w.kv === state.kv)
  return (
    <section id="cred-zk" className="mb-6" data-testid="agent-zk">
      <h4 className="mb-2 text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
        Who holds a key to this vault
      </h4>

      {state.kv === 0 ? (
        // Nothing to do here by hand: an agent with no key of its own yet (kv 0)
        // has each secret sealed to you and its approved machines as it arrives,
        // and approving a new machine gives it access (canopy-web#1550).
        <p className="text-[12px] text-foreground" data-testid="zk-private">
          Only you (your passkey) and the machines you approve can open these — canopy-web can&rsquo;t.
        </p>
      ) : (
        <div className="space-y-2 text-[12px]">
          <p className="text-foreground" data-testid="zk-on">
            Only you (your passkey) and the machines you approve can open these — canopy-web can&rsquo;t.
          </p>
          <table className="w-full text-[12px]" data-testid="zk-wraps">
            <thead>
              <tr className="text-left text-muted-foreground">
                <th className="py-1 font-medium">Can open them</th>
                <th className="py-1 font-medium" />
                <th className="py-1 font-medium" />
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {current.map((w) => (
                <tr key={`${w.kind}:${w.target ?? ''}`}>
                  <td className="py-1.5 text-foreground">
                    {w.kind === 'recovery' ? 'Recovery code' : w.kind === 'device' ? w.label.split(' · ')[0] : w.label}
                  </td>
                  <td className="py-1.5 text-muted-foreground">{KIND_LABEL[w.kind] ?? w.kind}</td>
                  <td className="py-1.5 text-right">
                    {!w.current && <span className="text-warning">no longer allowed — change the key</span>}
                  </td>
                </tr>
              ))}
              {pending.map((p) => (
                <tr key={`pending:${p.kind}:${p.target}`} data-testid={`zk-pending-${p.target}`}>
                  <td className="py-1.5 text-foreground">
                    {p.kind === 'device' ? p.runner_name || p.label || 'a machine' : p.email}
                  </td>
                  <td className="py-1.5 text-muted-foreground">{KIND_LABEL[p.kind] ?? p.kind}</td>
                  <td className="py-1.5 text-right text-warning">waiting</td>
                </tr>
              ))}
            </tbody>
          </table>
          {state.rotation_recommended && (
            <p className="text-warning" data-testid="zk-rotate-recommended">
              A machine or person who is no longer allowed could still open these. Change the key to shut them out.
            </p>
          )}
          <div className="flex flex-wrap gap-2">
            {pending.length > 0 && (
              <button type="button" disabled={busy !== null} data-testid="zk-grant"
                      onClick={() => void run('give access', () => giveAccess(slug, unlock))}
                      className="min-h-11 rounded-md bg-primary px-3 py-1 text-[12px] font-medium text-primary-foreground disabled:opacity-40 sm:min-h-0">
                {`Give ${pending.length === 1 ? 'them' : `all ${pending.length}`} access`}
              </button>
            )}
            <button type="button" disabled={busy !== null} data-testid="zk-rotate"
                    onClick={() => void run('rotate the vault key',
                      () => rotateVaultKey(slug, confirmCode(state.kv + 1), unlock), true)}
                    className="min-h-11 rounded-md border border-input px-3 py-1 text-[12px] text-foreground hover:border-primary disabled:opacity-40 sm:min-h-0">
              Change the key (lock out removed people)
            </button>
            {current.some((w) => w.kind === 'recovery') && <button type="button" disabled={busy !== null} data-testid="zk-recover"
                    onClick={() => setRecovering((v) => !v)}
                    className="min-h-11 rounded-md border border-input px-3 py-1 text-[12px] text-foreground hover:border-primary disabled:opacity-40 sm:min-h-0">
              Restore access with a recovery code
            </button>}
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
                Recover
              </button>
            </form>
          )}
        </div>
      )}

      {code && (
        <RecoveryCodeBox
          testId="zk-recovery-code" savedTestId="zk-code-saved" continueTestId="zk-code-continue"
          title={code.person ? 'Your recovery code — shown once' : 'Recovery code — shown once'}
          code={code.code}
          filename={code.person ? 'canopy-recovery-code.txt' : `canopy-vault-recovery-${slug}-kv${code.kv}.txt`}
          fileText={recoveryFile(slug, code.code, code.kv, code.person)}
          onContinue={() => closeCode(true)}
          onCancel={() => closeCode(false)}
        >
          {code.person
            ? 'If you lose your passkey and passphrase, this gets you back into every agent you hold. canopy-web does not keep a copy — save it in your password manager.'
            : 'It opens this agent\u2019s secrets if every machine and person loses access. canopy-web does not keep a copy. Save it before you continue.'}
        </RecoveryCodeBox>
      )}

      {own.element}
      {error && <p className="mt-2 text-[12px] text-destructive" data-testid="zk-error">{error}</p>}
      <details className="mt-2 text-[11px] text-muted-foreground">
        <summary className="cursor-pointer">How this is protected</summary>
        <p className="mt-1" data-testid="zk-trust-caveat">{ZK_TRUST_CAVEAT}</p>
      </details>
    </section>
  )
}
