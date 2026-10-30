import { useEffect, useState } from 'react'
import { Button } from 'canopy-ui/ui'

import { getPatRotation, giveCanopyLogin, type PatRotationOut } from '@/api/deviceKeys'
import { getKeyset } from '@/api/vault'
import { prepareRecoveryCode, vaultKeyForFlow, type PreparedRecovery } from '@/vault/operations'
import { browserSupportsPrf, PrfUnsupportedError, USE_A_PASSPHRASE } from '@/vault/prf'
import { useUnlockPrompt } from '@/vault/UnlockPrompt'

/**
 * Where this agent's secrets live, and its canopy login — the two things a new
 * agent needs before it can run, in one place, with no terminal.
 *
 * Its secrets live in the Canopy vault (only you and the machines you approve can
 * open it). The canopy login is the agent's own sign-in to canopy: one click makes
 * it and stores it straight in the vault — the browser never sees it — and it
 * renews itself.
 * Someone who has never set up their key does that first, inline: one passkey
 * touch and a recovery code to keep.
 */

// The agent's own canopy login: stored under the variable name its code reads
// (`apps/agents/pat_rotation.py::CANOPY_PAT_REF`).
const LOGIN_REF = 'CANOPY_WEB_PAT'

export function AgentSecretsHomeSection({
  slug,
  storedNames,
  canEdit,
  onChanged,
  show = 'all',
}: {
  slug: string
  storedNames: string[]
  canEdit: boolean
  onChanged?: () => void
  /** Which half: where the secrets live (the vault's), the canopy login (the agent's), or both. */
  show?: 'source' | 'login' | 'all'
}) {
  const [rotation, setRotation] = useState<PatRotationOut | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [note, setNote] = useState('')
  const [code, setCode] = useState<PreparedRecovery | null>(null)
  const [saved, setSaved] = useState(false)
  const { prompt, element } = useUnlockPrompt()
  const hasLogin = storedNames.includes(LOGIN_REF)

  useEffect(() => {
    let cancelled = false
    if (!canEdit) return
    getPatRotation(slug)
      .then((r) => !cancelled && setRotation(r))
      .catch(() => undefined)
    return () => {
      cancelled = true
    }
  }, [slug, canEdit, hasLogin])

  const fail = (e: unknown, fallback: string) =>
    setError(e instanceof PrfUnsupportedError ? USE_A_PASSPHRASE
      : e instanceof Error && e.name === 'NotAllowedError' ? 'The passkey prompt was dismissed.'
        : e instanceof Error ? e.message : fallback)

  const mint = async () => {
    const out = await giveCanopyLogin(slug)
    setNote(out.rotation_enabled
      ? `Done — it signs in to canopy as ${out.user_email}, and the login renews itself.`
      : `Done — it signs in to canopy as ${out.user_email}.${out.rotation_note ? ` It won't renew on its own yet: ${out.rotation_note}` : ''}`)
    onChanged?.()
  }

  const give = async () => {
    setBusy(true)
    setError('')
    setNote('')
    try {
      const ks = await getKeyset()
      if (!ks.exists) {
        // Nobody could open the stored login yet: set up the person's key first.
        const got = await vaultKeyForFlow(await browserSupportsPrf(), prompt)
        if (!got) {
          setError('This browser’s passkey can’t hold your key. Set it up with a passphrase under Settings → Security → You, then come back.')
          return
        }
        setSaved(false)
        setCode(await prepareRecoveryCode(got.me))
        return // continues in keepCode, once the code is saved
      }
      await mint()
    } catch (e) {
      fail(e, 'Could not give it its canopy login.')
    } finally {
      setBusy(false)
    }
  }

  const keepCode = async () => {
    if (!code) return
    setBusy(true)
    setError('')
    try {
      await code.commit()
      setCode(null)
      await mint()
    } catch (e) {
      fail(e, 'Could not finish.')
    } finally {
      setBusy(false)
    }
  }

  const download = () => {
    if (!code) return
    const text = `Canopy recovery code\n\n${code.code}\n\nIf you lose your passkey, this code gets you back into your agents' secrets. Keep it offline.\n`
    const url = URL.createObjectURL(new Blob([text], { type: 'text/plain' }))
    const a = document.createElement('a')
    a.href = url
    a.download = 'canopy-recovery-code.txt'
    a.click()
    URL.revokeObjectURL(url)
  }

  return (
    <section id={show === 'source' ? 'secrets-home' : 'canopy-login'} className="mb-6 scroll-mt-6 space-y-3" data-testid="agent-secrets-home">
      {show !== 'login' && <div>
        <h4 className="text-[13px] font-semibold text-foreground">Where its secrets live</h4>
        <p className="mt-1.5 text-[12px] text-muted-foreground" data-testid="secrets-home-explain">
          In the Canopy vault. Only you and the machines you approve can open them — canopy-web can’t.
        </p>
      </div>}

      {show !== 'source' && (
        <div>
          <h4 className="text-[13px] font-semibold text-foreground">Its canopy login</h4>
          {hasLogin ? (
            <p className="mt-1 text-[12px]" data-testid="canopy-login-ok">
              <span className="text-success">Stored ✓</span>
              <span className="text-muted-foreground">
                {rotation?.enabled
                  ? ` — renews itself every ${rotation.period_days} days.`
                  : ' — it doesn’t renew on its own.'}
              </span>
            </p>
          ) : (
            <p className="mt-1 text-[12px] text-muted-foreground">
              How it signs in to canopy to read its tasks, send its email and report back. One click makes it and
              keeps it in the vault; it renews itself.
            </p>
          )}
          {canEdit && !code && (!hasLogin || !rotation?.enabled) && (
            <Button size="sm" className="mt-2" disabled={busy} onClick={() => void give()} data-testid="canopy-login-give">
              {busy ? 'Working…' : hasLogin ? 'Make a fresh login' : 'Give it its canopy login'}
            </Button>
          )}
        </div>
      )}

      {code && (
        <div role="dialog" aria-label="Recovery code" data-testid="canopy-login-code"
             className="space-y-2 rounded-lg border border-warning/40 bg-warning/10 p-3 text-sm">
          <p className="font-medium text-foreground">Your recovery code — shown once</p>
          <p className="text-muted-foreground">
            Your key is set up. If you lose your passkey, this code gets you back into your agents’ secrets. canopy-web
            doesn’t keep a copy — save it in your password manager.
          </p>
          <code className="block select-all break-all font-mono text-foreground">{code.code}</code>
          <div className="flex flex-wrap items-center gap-2">
            <Button size="sm" variant="outline" onClick={download}>Download</Button>
            <label className="flex items-center gap-1 text-foreground">
              <input type="checkbox" checked={saved} onChange={(e) => setSaved(e.target.checked)} data-testid="canopy-login-saved" />
              I have saved it
            </label>
            <Button size="sm" disabled={!saved || busy} onClick={() => void keepCode()} data-testid="canopy-login-continue">
              Continue
            </Button>
          </div>
        </div>
      )}

      {note && <p className="text-[12px] text-success" data-testid="canopy-login-note">{note}</p>}
      {error && <p className="text-[12px] text-destructive" data-testid="secrets-home-error">{error}</p>}
      {element}
    </section>
  )
}
