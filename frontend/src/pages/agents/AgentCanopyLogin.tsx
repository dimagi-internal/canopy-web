import { useEffect, useState } from 'react'
import { Button } from 'canopy-ui/ui'

import { getAgentCredentialStatus } from '@/api/agents'
import {
  getPatRotation, giveCanopyLogin, rotatePatNow, setPatRotation, type PatRotationOut,
} from '@/api/deviceKeys'
import { getKeyset } from '@/api/vault'
import { relativeAge } from '@/lib/relativeAge'
import { prepareRecoveryCode, vaultKeyForFlow, type PreparedRecovery } from '@/vault/operations'
import { browserSupportsPrf, PrfUnsupportedError, USE_A_PASSPHRASE } from '@/vault/prf'
import { useUnlockPrompt } from '@/vault/UnlockPrompt'

/**
 * The agent's own sign-in to canopy — how it reads its tasks, sends its email and
 * reports back. One click makes it and stores it straight in the vault (the browser
 * never sees it); canopy-web then renews it on a schedule, the old one working for
 * 24 hours after. Someone who has never set up their key does that first, inline: one
 * passkey touch and a recovery code to keep.
 *
 * One block: whether it exists, whether it renews, and one way to replace it now.
 */

// The variable name the agent's code reads it under (`apps/agents/credentials/pat_rotation.py::CANOPY_PAT_REF`).
export const CANOPY_LOGIN_REF = 'CANOPY_WEB_PAT'

export function AgentCanopyLogin({
  slug,
  canEdit,
  reloadKey = 0,
  onChanged,
}: {
  slug: string
  canEdit: boolean
  /** Bumped when the vault's secrets changed, so this re-reads. */
  reloadKey?: number
  /** The login was made or replaced: the vault's table should show it. */
  onChanged?: () => void
}) {
  const [hasLogin, setHasLogin] = useState<boolean | null>(null)
  const [rotation, setRotation] = useState<PatRotationOut | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [note, setNote] = useState('')
  const [code, setCode] = useState<PreparedRecovery | null>(null)
  const [saved, setSaved] = useState(false)
  const { prompt, element } = useUnlockPrompt()

  useEffect(() => {
    let cancelled = false
    getAgentCredentialStatus(slug).then(
      (rows) => !cancelled && setHasLogin(rows.some((r) => r.name === CANOPY_LOGIN_REF && r.set)),
      () => undefined,
    )
    if (canEdit) getPatRotation(slug).then((r) => !cancelled && setRotation(r), () => undefined)
    return () => {
      cancelled = true
    }
  }, [slug, canEdit, reloadKey])

  const fail = (e: unknown, fallback: string) =>
    setError(e instanceof PrfUnsupportedError ? USE_A_PASSPHRASE
      : e instanceof Error && e.name === 'NotAllowedError' ? 'The passkey prompt was dismissed.'
        : e instanceof Error ? e.message : fallback)

  const run = async (fn: () => Promise<void>, fallback: string) => {
    setBusy(true)
    setError('')
    setNote('')
    try {
      await fn()
    } catch (e) {
      fail(e, fallback)
    } finally {
      setBusy(false)
    }
  }

  const mint = async () => {
    const out = await giveCanopyLogin(slug)
    setNote(out.rotation_enabled
      ? `Done — it signs in to canopy as ${out.user_email}, and the login renews itself.`
      : `Done — it signs in to canopy as ${out.user_email}.${out.rotation_note ? ` It won't renew on its own yet: ${out.rotation_note}` : ''}`)
    setHasLogin(true)
    onChanged?.()
  }

  const give = () => run(async () => {
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
  }, 'Could not give it its canopy login.')

  const keepCode = () => run(async () => {
    if (!code) return
    await code.commit()
    setCode(null)
    await mint()
  }, 'Could not finish.')

  const renew = (enabled: boolean) => run(async () => {
    if (!rotation) return
    setRotation(await setPatRotation(slug, enabled, rotation.period_days))
  }, 'Could not change renewal.')

  const replaceNow = () => {
    if (!window.confirm('Make a new login now? The old one keeps working for 24 hours.')) return
    // Renewing is the graceful way; with it off, making one afresh is the same thing.
    if (rotation?.enabled) void run(async () => { setRotation(await rotatePatNow(slug)); onChanged?.() }, 'Could not make a new login.')
    else void give()
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

  const last = rotation?.history[0]
  return (
    <section id="canopy-login" className="scroll-mt-6 space-y-2" data-testid="agent-canopy-login">
      <span id="cred-pat-rotation" />
      {hasLogin === null ? null : hasLogin ? (
        <p className="text-[12px]" data-testid="canopy-login-ok">
          <span className="text-success">Stored ✓</span>
          <span className="text-muted-foreground"> — it signs in to canopy with its own login, kept in the vault.</span>
        </p>
      ) : (
        <p className="text-[12px] text-muted-foreground">
          It has no canopy login yet. That is how it signs in to canopy to read its tasks, send its email and report
          back. One click makes it and keeps it in the vault; it renews itself.
        </p>
      )}

      {canEdit && !hasLogin && hasLogin !== null && !code && (
        <Button size="sm" disabled={busy} onClick={() => void give()} data-testid="canopy-login-give">
          {busy ? 'Working…' : 'Give it its canopy login'}
        </Button>
      )}

      {canEdit && hasLogin && rotation && !code && (
        <div className="space-y-1">
          <div className="flex flex-wrap items-center gap-3 text-[12px] text-foreground">
            <label className="flex items-center gap-2">
              <input
                type="checkbox"
                checked={rotation.enabled}
                disabled={busy}
                onChange={() => void renew(!rotation.enabled)}
                aria-label="Renew this login automatically"
                data-testid="pat-rotation-enabled"
              />
              Renew it automatically, every {rotation.period_days} days
            </label>
            <button
              type="button"
              disabled={busy}
              onClick={replaceNow}
              data-testid="pat-rotate-now"
              className="min-h-11 rounded-md border border-input px-3 py-1 text-[12px] text-foreground hover:border-primary disabled:opacity-40 sm:min-h-0"
            >
              Make a new login now
            </button>
          </div>
          <p className="text-[11px] text-muted-foreground" data-testid="pat-rotation-summary">
            {rotation.last_rotated_at ? `Last renewed ${relativeAge(rotation.last_rotated_at)}` : 'Not renewed yet'}
            {rotation.enabled && rotation.next_due_at ? ` · next ${new Date(rotation.next_due_at).toLocaleDateString()}` : ''}
            {!rotation.enabled && ' · it will stop working when it expires'}
            {last?.status === 'failed' && <span className="text-destructive"> · last attempt failed: {last.detail}</span>}
          </p>
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
      {error && <p className="text-[12px] text-destructive" data-testid="canopy-login-error">{error}</p>}
      {element}
    </section>
  )
}
