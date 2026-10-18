import { useEffect, useState, type ReactNode } from 'react'
import { Button } from 'canopy-ui/ui'
import { getKeyset, type VaultKeysetOut } from '@/api/vault'
import { registerThisDevice } from '@/api/passkeys'
import { defaultLabel } from '@/auth/webauthn'
import {
  checkRecoveryCode,
  hasRecoveryCode,
  prepareRecoveryCode,
  recoverVaultKey,
  unlockVaultKey,
  vaultKeyForFlow,
  type PreparedRecovery,
} from '@/vault/operations'
import { browserSupportsPrf, PrfUnsupportedError, USE_A_PASSPHRASE } from '@/vault/prf'
import { useUnlockPrompt } from '@/vault/UnlockPrompt'
import { PasskeysPanel } from './PasskeysPanel'
import { useHash } from '@/lib/useHash'
import { EmergencyKit, VaultKeyPanel } from './VaultKeyPanel'
import { ZkAgentsPanel } from './ZkAgentsPanel'

/**
 * Security — one card for everything that keeps agents' secrets safe
 * (canopy-web#1550): You (how you confirm it's you, your recovery code),
 * Your machines (approve or revoke), Your agents (where each stands). The old
 * separate Passkeys / Device keys / Vault key / Zero-knowledge cards were one
 * idea split four ways. Deep links still land: #passkeys, #vault-key,
 * #device-keys, #your-agents.
 */
export function SecurityCard({ machines }: { machines: ReactNode }) {
  return (
    <div id="security" className="rounded-xl border border-border bg-card p-5 space-y-5" data-testid="settings-security">
      <div>
        <h2 className="text-sm font-semibold text-foreground">Security</h2>
        <p className="mt-1 text-sm text-foreground-secondary" data-testid="security-summary">
          Your agents&rsquo; secrets can be opened by: <strong className="font-medium text-foreground">you</strong> (your
          passkey) · <strong className="font-medium text-foreground">machines you approve</strong> · not canopy-web.
        </p>
        <p className="mt-1 text-xs text-muted-foreground">
          canopy-web stores each agent&rsquo;s secrets locked. A copy of the key goes to you and to each machine you
          approve; a machine&rsquo;s copy is kept inside its own hardware and can&rsquo;t be copied off it.
        </p>
      </div>

      <Row id="you" title="You">
        <PasskeysPanel />
        <RecoveryRow />
      </Row>

      <Row id="your-machines" title="Your machines"
           hint="Each can open the agents it runs. Approving a machine — one passkey tap — hands it its copy of the key.">
        {machines}
      </Row>

      <Row id="your-agents" title="Your agents">
        <ZkAgentsPanel />
      </Row>
    </div>
  )
}

function Row({ id, title, hint, children }: { id: string; title: string; hint?: string; children: ReactNode }) {
  return (
    <section id={id} className="space-y-2 border-t border-border pt-4 first-of-type:border-t-0 first-of-type:pt-0"
             data-testid={`security-${id}`}>
      <h3 className="text-[11px] font-medium uppercase tracking-wide text-muted-foreground">{title}</h3>
      {hint && <p className="text-xs text-muted-foreground">{hint}</p>}
      {children}
    </section>
  )
}

/** Your one recovery code: whether you have it, making it, using it. */
function RecoveryRow() {
  const hash = useHash()
  const { prompt, element } = useUnlockPrompt()
  const [ks, setKs] = useState<VaultKeysetOut | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [made, setMade] = useState<PreparedRecovery | null>(null)
  const [saved, setSaved] = useState(false)
  const [more, setMore] = useState(false)
  const [recovering, setRecovering] = useState(false)
  const [typed, setTyped] = useState('')
  const [newPass, setNewPass] = useState('')
  const [kit, setKit] = useState<string | null>(null)
  const [checking, setChecking] = useState(false)
  const [checkTyped, setCheckTyped] = useState('')
  const [checked, setChecked] = useState<{ ok: boolean; text: string } | null>(null)

  const load = () => getKeyset().then(setKs).catch(() => setKs(null))
  useEffect(() => {
    void load()
  }, [])
  useEffect(() => {
    if (hash === '#vault-key') setMore(true)
  }, [hash])

  const fail = (e: unknown, fallback: string) =>
    setError(e instanceof PrfUnsupportedError ? USE_A_PASSPHRASE
      : e instanceof Error && e.name === 'NotAllowedError' ? 'The passkey prompt was dismissed.'
        : e instanceof Error ? e.message : fallback)

  const create = async () => {
    setBusy(true)
    setError('')
    try {
      setSaved(false)
      if (!ks?.exists) {
        // First time: one passkey touch makes the key the agents' secrets are
        // sealed to, then the recovery code for it.
        const got = await vaultKeyForFlow(await browserSupportsPrf(), prompt)
        if (!got) {
          setMore(true) // no passkey here can hold it: the passphrase option, below
          return
        }
        setMade(await prepareRecoveryCode(got.me))
        return
      }
      setMade(await prepareRecoveryCode(await unlockVaultKey(prompt)))
    } catch (e) {
      fail(e, 'Could not make a recovery code.')
    } finally {
      setBusy(false)
    }
  }

  const keep = async () => {
    if (!made) return
    setBusy(true)
    try {
      setKs(await made.commit())
      setMade(null)
    } catch (e) {
      fail(e, 'Could not save the recovery code.')
    } finally {
      setBusy(false)
    }
  }

  /** Try the saved code without using it: opened here, then discarded. */
  const check = async () => {
    setBusy(true)
    setChecked(null)
    try {
      const out = await checkRecoveryCode(checkTyped)
      setChecked(out.result === 'works' ? { ok: true, text: 'This code works ✓' }
        : out.result === 'malformed' ? { ok: false, text: `That doesn’t look like a recovery code: ${out.detail}` }
          : out.result === 'none' ? { ok: false, text: 'You have no recovery code on file.' }
            : { ok: false, text: 'This code doesn’t match. Look for a newer one, or create a new recovery code.' })
    } catch (e) {
      fail(e, 'Could not check the code.')
    } finally {
      setBusy(false)
    }
  }

  /** Lost your passkey: a new passkey on this device, opened by the code. */
  const recover = async (how: 'passkey' | 'passphrase') => {
    setBusy(true)
    setError('')
    try {
      if (how === 'passkey') {
        // Registering proves it's you (the step-up), and the new passkey then holds the key.
        await registerThisDevice(defaultLabel(), { upgrade: true })
        setKs((await recoverVaultKey(typed, { passkey: true })).keyset)
      } else {
        const out = await recoverVaultKey(typed, { passphrase: newPass })
        setKs(out.keyset)
        setKit(out.secretKey ?? null)
      }
      setRecovering(false)
      setTyped('')
      setNewPass('')
    } catch (e) {
      fail(e, 'Could not recover.')
    } finally {
      setBusy(false)
    }
  }

  const download = () => {
    if (!made) return
    const text = `Canopy recovery code\n\n${made.code}\n\nIf you lose your passkey and passphrase, this gets you back into every agent you hold. Keep it offline.\n`
    const url = URL.createObjectURL(new Blob([text], { type: 'text/plain' }))
    const a = document.createElement('a')
    a.href = url
    a.download = 'canopy-recovery-code.txt'
    a.click()
    URL.revokeObjectURL(url)
  }

  if (ks === null) return null
  const exists = Boolean(ks.exists)
  return (
    <div className="space-y-2 text-sm" data-testid="security-recovery">
      {!exists ? (
        !made && (
          <div className="flex flex-wrap items-center gap-2" data-testid="security-no-key">
            <span className="text-muted-foreground">
              Set up once: one passkey touch, and a recovery code to keep. After that your agents&rsquo; secrets stay
              private to you on their own.
            </span>
            <Button size="sm" disabled={busy} onClick={() => void create()} data-testid="security-setup">
              {busy ? 'Working…' : 'Set up'}
            </Button>
          </div>
        )
      ) : hasRecoveryCode(ks) ? (
        <p data-testid="security-recovery-ok">
          <span className="text-foreground">Recovery code</span>{' '}
          <span className="text-success">saved ✓</span>
          <span className="text-muted-foreground"> — your spare: if you lose your passkey, this code gets you back in.</span>
        </p>
      ) : !made && (
        <div className="flex flex-wrap items-center gap-2" data-testid="security-recovery-missing">
          <span className="text-warning">No recovery code yet — your spare if you lose your passkey.</span>
          <Button size="sm" variant="outline" disabled={busy} onClick={() => void create()} data-testid="security-recovery-create">
            {busy ? 'Working…' : 'Create your recovery code'}
          </Button>
        </div>
      )}

      {made && (
        <div role="dialog" aria-label="Recovery code" className="space-y-2 rounded-lg border border-warning/40 bg-warning/10 p-3"
             data-testid="security-recovery-code">
          <p className="font-medium text-foreground">Your recovery code — shown once</p>
          <p className="text-muted-foreground">
            Your spare: if you lose your passkey, this code gets you back into every agent you hold. canopy-web does
            not keep a copy — save it in your password manager.
          </p>
          <code className="block select-all break-all font-mono text-foreground">{made.code}</code>
          <div className="flex flex-wrap items-center gap-2">
            <Button size="sm" variant="outline" onClick={download}>Download</Button>
            <label className="flex items-center gap-1 text-foreground">
              <input type="checkbox" checked={saved} onChange={(e) => setSaved(e.target.checked)} data-testid="security-recovery-saved" />
              I have saved it
            </label>
            <Button size="sm" disabled={!saved || busy} onClick={() => void keep()} data-testid="security-recovery-keep">Done</Button>
            <Button size="sm" variant="ghost" disabled={busy} onClick={() => setMade(null)}>Cancel</Button>
          </div>
        </div>
      )}

      {kit && <EmergencyKit secretKey={kit} email="" onDone={() => setKit(null)} />}

      {exists && (
        <div className="flex flex-wrap gap-3 text-xs">
          <button type="button" className="text-primary hover:underline" onClick={() => setMore((v) => !v)}
                  data-testid="security-more">
            {more ? 'Fewer options' : 'More options'}
          </button>
          {hasRecoveryCode(ks) && (
            <button type="button" className="text-primary hover:underline"
                    onClick={() => { setChecking((v) => !v); setChecked(null); setCheckTyped('') }}
                    data-testid="security-check">
              Check my recovery code
            </button>
          )}
          {hasRecoveryCode(ks) && (
            <button type="button" className="text-primary hover:underline" onClick={() => setRecovering((v) => !v)}
                    data-testid="security-recover">
              Lost your passkey?
            </button>
          )}
        </div>
      )}

      {checking && (
        <div className="space-y-2" data-testid="security-check-form">
          <p className="text-xs text-muted-foreground">
            Paste the code you saved to make sure it works. It is checked here in your browser and nothing changes.
          </p>
          <div className="flex flex-wrap items-center gap-2">
            <input value={checkTyped} onChange={(e) => { setCheckTyped(e.target.value); setChecked(null) }}
                   autoComplete="off" spellCheck={false} placeholder="Your recovery code" aria-label="Recovery code to check"
                   className="min-h-11 w-full rounded-md border border-input bg-input px-2 py-1 font-mono text-[12px] text-foreground sm:min-h-0 sm:w-96" />
            <Button size="sm" variant="outline" disabled={busy || !checkTyped.trim()} onClick={() => void check()}
                    data-testid="security-check-go">
              Check
            </Button>
          </div>
          {checked && (
            <p className={`text-xs ${checked.ok ? 'text-success' : 'text-warning'}`} data-testid="security-check-result">
              {checked.text}
            </p>
          )}
        </div>
      )}

      {recovering && (
        <div className="space-y-2" data-testid="security-recover-form">
          <input value={typed} onChange={(e) => setTyped(e.target.value)} autoComplete="off" spellCheck={false}
                 placeholder="Your recovery code" aria-label="Recovery code"
                 className="min-h-11 w-full rounded-md border border-input bg-input px-2 py-1 font-mono text-[12px] text-foreground sm:min-h-0 sm:w-96" />
          <div className="flex flex-wrap items-center gap-2">
            <Button size="sm" disabled={busy || !typed.trim()} onClick={() => void recover('passkey')}
                    data-testid="security-recover-passkey">
              Use a new passkey on this device
            </Button>
            <span className="text-xs text-muted-foreground">or</span>
            <input type="password" value={newPass} onChange={(e) => setNewPass(e.target.value)} autoComplete="new-password"
                   placeholder="A new passphrase" aria-label="New passphrase"
                   className="min-h-11 rounded-md border border-input bg-input px-2 py-1 text-sm text-foreground sm:min-h-0 sm:w-56" />
            <Button size="sm" variant="outline" disabled={busy || !typed.trim() || !newPass}
                    onClick={() => void recover('passphrase')} data-testid="security-recover-passphrase">
              Use a new passphrase
            </Button>
          </div>
        </div>
      )}

      {more && <VaultKeyPanel embedded />}
      {error && <p className="text-xs text-destructive" data-testid="security-error">{error}</p>}
      {element}
    </div>
  )
}
