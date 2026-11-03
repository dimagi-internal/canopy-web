import { useEffect, useState } from 'react'
import { Button } from 'canopy-ui/ui'
import { getKeyset, type VaultKeysetOut } from '@/api/vault'
import { myKeyCopies, type MyKeyCopy } from '@/api/chat'
import { atStake, atStakeLost, replaceKeepingConversations } from '@/vault/conversationCopies'
import { readNewKeyImpact, type NewKeyImpact } from '@/vault/newKeyImpact'
import {
  createVaultKeyWithPasskey,
  createVaultKeyWithPassphrase,
  stepUpAndUnlock,
  unlockVaultKey,
  type UnlockPrompt,
} from '@/vault/operations'
import { keyFlowError, PrfUnsupportedError } from '@/vault/prf'
import { passphraseProblem } from '@/vault/zk'

/**
 * "Create a new vault key": one screen, for someone who may have lost everything that
 * opens the current one. It says what the new key costs (which vaults, who can let them
 * back in), asks about private conversations only if they are in some, and works with a
 * passkey that gives no PRF output: then the new key is sealed by a new passphrase, and
 * the old key is never asked for.
 */

type Carry = 'carry' | 'without'

function noticeFor(out: { carried: number; lost: MyKeyCopy[] }, carry: boolean): string {
  if (out.lost.length) {
    return `${out.lost.length} private conversation${out.lost.length === 1 ? '' : 's'} ${
      carry ? 'could not be carried over (your copy was already out of date)' : 'not carried over'}. `
      + 'Ask someone in each to give you a fresh copy (People → Give key).' + atStakeLost(out.lost)
  }
  if (out.carried) return `Your ${out.carried} private conversation${out.carried === 1 ? ' was' : 's were'} carried over to the new key.`
  return ''
}

export function NewVaultKey({ userId, passkeyOk, prompt, onCreated, onCancel }: {
  userId: string
  /** False: this browser or these passkeys cannot hold a key, so ask for a passphrase from the start. */
  passkeyOk: boolean
  prompt: UnlockPrompt
  onCreated: (keyset: VaultKeysetOut, secretKey: string | null, notice: string) => void
  onCancel: () => void
}) {
  const [copies, setCopies] = useState<MyKeyCopy[] | null>(null)
  const [impact, setImpact] = useState<NewKeyImpact | null>(null)
  const [carry, setCarry] = useState<Carry | null>(null)
  const [noPrf, setNoPrf] = useState(false)
  const [pass1, setPass1] = useState('')
  const [pass2, setPass2] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => {
    let cancelled = false
    myKeyCopies()
      .then((c) => !cancelled && setCopies(c))
      .catch((e: unknown) => !cancelled && setError(`Could not list your private conversations, so nothing was changed: ${
        e instanceof Error ? e.message : 'the request failed'}.`))
    readNewKeyImpact(userId)
      .then((i) => !cancelled && setImpact(i))
      .catch(() => !cancelled && setImpact({ vaults: [], unread: ['your vaults'] }))
    return () => {
      cancelled = true
    }
  }, [userId])

  const withPassphrase = !passkeyOk || noPrf
  const problem = pass1 ? passphraseProblem(pass1) : null
  const mismatch = pass2 !== '' && pass1 !== pass2
  const passphraseReady = !withPassphrase || (pass1 !== '' && !problem && pass1 === pass2)
  const ready = copies !== null && impact !== null && passphraseReady && (copies.length === 0 || carry !== null)

  const create = async () => {
    if (!ready || copies === null) return
    const carrying = copies.length > 0 && carry === 'carry'
    setBusy(true)
    setError('')
    let made = false
    let secretKey: string | null = null
    const unlockOld = async () => {
      try {
        return (await stepUpAndUnlock(await getKeyset())) ?? await unlockVaultKey(prompt)
      } catch (e) {
        if (!(e instanceof PrfUnsupportedError)) throw e
        // Not the same fix as below: a new passphrase cannot open the OLD key.
        throw new Error('This passkey cannot unlock your current key, so nothing was changed. Choose '
          + '“I can’t unlock it” to create the key without carrying them over.', { cause: e })
      }
    }
    const makeNew = async () => {
      if (withPassphrase) {
        const out = await createVaultKeyWithPassphrase(pass1, true)
        made = true
        secretKey = out.secretKey
        return out
      }
      const out = await createVaultKeyWithPasskey(true)
      made = true
      return out
    }
    try {
      const out = await replaceKeepingConversations(unlockOld, makeNew, copies, { carry: carrying || copies.length === 0 })
      onCreated(out.keyset, secretKey, noticeFor(out, carrying))
    } catch (e) {
      if (made) {
        // The key IS replaced (only carrying failed): this may be the one chance to show its Secret Key.
        const message = e instanceof Error ? e.message : 'Your new vault key is set.'
        onCreated(await getKeyset(), secretKey, message)
      } else if (e instanceof PrfUnsupportedError) {
        setNoPrf(true)
        setError('Your passkey can’t hold a vault key, so nothing was changed. Choose a new passphrase instead; '
          + 'you will also get a new Secret Key.')
      } else {
        setError(keyFlowError(e, 'Something went wrong. Nothing was changed.'))
      }
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="space-y-3 rounded-lg border border-border bg-card p-3 text-[12px]" data-testid="new-key">
      <p className="font-medium text-foreground">Create a new vault key</p>
      <p className="text-foreground" data-testid="new-key-lead">
        This does not need your current passphrase, Secret Key or passkey, and nothing stored in any vault is deleted.
        We may ask you to touch a passkey on this account, only to confirm it is you.
      </p>
      <p className="text-muted-foreground">
        Your copy of each vault below stops opening until someone lets you back in. Your recovery code, if you made one,
        stops working.
      </p>

      {impact === null ? (
        <p className="text-muted-foreground" data-testid="new-key-loading">Checking which vaults you hold a copy of…</p>
      ) : (
        <div className="space-y-1">
          {impact.vaults.length === 0 ? (
            <p className="text-muted-foreground" data-testid="new-key-no-vaults">You hold no copy of any vault we could read.</p>
          ) : (
            <ul className="space-y-1" data-testid="new-key-vaults">
              {impact.vaults.map((v) => (
                <li key={v.id} data-testid={`new-key-vault-${v.id}`}>
                  <span className="text-foreground">{v.name}</span>
                  <span className="text-muted-foreground"> ({v.kind === 'agent' ? 'agent vault' : 'workspace vault'}): </span>
                  {v.alone ? (
                    <span className="font-medium text-warning" data-testid="new-key-alone">
                      only you can open it. Check with an owner or admin first.
                    </span>
                  ) : (
                    <span className="text-muted-foreground">{v.letsYouBackIn.join(', ')} can let you back in.</span>
                  )}
                </li>
              ))}
            </ul>
          )}
          {impact.unread.length > 0 && (
            <p className="text-warning" data-testid="new-key-unread">
              Could not read {impact.unread.join(', ')}. You may hold a copy of those too.
            </p>
          )}
        </div>
      )}

      {copies !== null && copies.length > 0 && (
        <fieldset className="space-y-1" data-testid="new-key-conversations">
          <legend className="whitespace-pre-line text-muted-foreground">{atStake(copies).trim()}</legend>
          <label className="flex items-center gap-2 text-foreground">
            <input type="radio" name="new-key-carry" checked={carry === 'carry'} disabled={busy}
                   onChange={() => setCarry('carry')} data-testid="new-key-carry" />
            I can unlock my current key, carry them over
          </label>
          <label className="flex items-center gap-2 text-foreground">
            <input type="radio" name="new-key-carry" checked={carry === 'without'} disabled={busy}
                   onChange={() => setCarry('without')} data-testid="new-key-without" />
            I can&rsquo;t unlock it, create the key without carrying them over
          </label>
          {carry === 'without' && (
            <p className="text-muted-foreground" data-testid="new-key-without-note">
              They stay as they are for everyone else. Anyone in each conversation can give you a fresh copy (People → Give key).
            </p>
          )}
        </fieldset>
      )}

      {withPassphrase && (
        <div className="space-y-1" data-testid="new-key-passphrase">
          <div className="flex flex-wrap gap-2">
            <input type="password" value={pass1} onChange={(e) => setPass1(e.target.value)} autoComplete="new-password"
                   aria-label="New vault passphrase" placeholder="New passphrase (12+ characters)"
                   className="min-h-11 w-full rounded-md border border-input bg-input px-2 py-1 text-sm text-foreground sm:min-h-0 sm:w-64" />
            <input type="password" value={pass2} onChange={(e) => setPass2(e.target.value)} autoComplete="new-password"
                   aria-label="Repeat the passphrase" placeholder="Repeat it"
                   className="min-h-11 w-full rounded-md border border-input bg-input px-2 py-1 text-sm text-foreground sm:min-h-0 sm:w-64" />
          </div>
          {(problem || mismatch) && (
            <p className="text-warning" data-testid="new-key-passphrase-problem">{problem ?? 'The two do not match.'}</p>
          )}
          <p className="text-muted-foreground">You will get a new Secret Key and Emergency Kit to save.</p>
        </div>
      )}

      {error && <p className="text-destructive" data-testid="new-key-error">{error}</p>}

      <div className="flex flex-wrap gap-2">
        <Button size="sm" disabled={busy || !ready} onClick={() => void create()} data-testid="new-key-create">
          {busy ? 'Working…' : 'Create a new vault key'}
        </Button>
        <Button size="sm" variant="ghost" disabled={busy} onClick={onCancel} data-testid="new-key-cancel">Cancel</Button>
      </div>
    </div>
  )
}
