import { useCallback, useEffect, useState } from 'react'
import { KeyRound, Loader2, ShieldCheck, TriangleAlert } from 'lucide-react'
import {
  getKeyset,
  getSecretRequest,
  getZkRecipients,
  type SecretRequestOut,
  type ZkRecipientsOut,
} from '@/api/vault'
import { endorseReaders, sealAndStore, stepUpAndUnlock, unlockVaultKey, type Unlocked } from '@/vault/operations'
import { assess, changedKey, loadPins, newEndorsers, savePins, type Assessment, type Endorser } from '@/vault/trust'
import { useUnlockPrompt } from '@/vault/UnlockPrompt'
import { fingerprint } from '@/vault/zk'

/**
 * Give an agent a secret — one password field, sealed in THIS browser.
 *
 * Spec: docs/superpowers/specs/2026-10-19-give-an-agent-a-secret-design.md.
 * The value is sealed (`zks1`) to the agent's readers before anything leaves
 * the page, and only to readers vouched for by a key this browser trusts
 * (`@/vault/trust`): the person's own vault key, or someone they pinned. The
 * session that asked never sees it; neither does canopy-web.
 *
 * With `requestId` it answers an agent's request (the chat card); without, it
 * is an admin adding a declared secret (the setup checklist).
 */
export function SecretSealCard({ slug, name, requestId, onStored }: {
  slug: string
  name: string
  requestId?: string
  onStored?: (name: string) => void
}) {
  const unlocker = useUnlockPrompt()
  const [req, setReq] = useState<SecretRequestOut | null>(null)
  const [value, setValue] = useState('')
  const [phase, setPhase] = useState<'idle' | 'checking' | 'trust' | 'confirm' | 'sealing' | 'done'>('idle')
  const [error, setError] = useState('')
  const [me, setMe] = useState<Unlocked | null>(null)
  const [data, setData] = useState<ZkRecipientsOut | null>(null)
  const [verdict, setVerdict] = useState<Assessment | null>(null)
  const [sealedTo, setSealedTo] = useState(0)

  useEffect(() => {
    if (!requestId) return
    let live = true
    getSecretRequest(slug, requestId).then((r) => live && setReq(r), (e: unknown) =>
      live && setError(e instanceof Error ? e.message : 'Could not load the request'))
    return () => {
      live = false
    }
  }, [slug, requestId])

  const closed = req && req.status !== 'open'

  /** Seal and send, once `a` says who. The plaintext leaves state first. */
  const seal = useCallback(async (a: Assessment) => {
    const plain = value
    setValue('')
    setPhase('sealing')
    try {
      await sealAndStore(slug, name, plain, a, requestId)
      setSealedTo(a.sealTo.length)
      setPhase('done')
      if (requestId) setReq(await getSecretRequest(slug, requestId))
      onStored?.(name)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not store the value')
      setPhase('idle')
    }
  }, [value, slug, name, requestId, onStored])

  /** Decide who to seal to; stop to ask when the person must decide. */
  const decide = useCallback(async (d: ZkRecipientsOut, self: Unlocked | null, accept = new Set<string>()) => {
    const a = await assess(d, { self: self ? { userId: self.userId, publicJwk: self.publicJwk } : null,
                                pins: loadPins(), accept })
    setVerdict(a)
    const changed = changedKey(a)
    if (changed) {
      setError(`${changed.email || 'Someone'}'s vault key changed since this browser last trusted it `
        + `(was ${fingerprint(changed.was ?? '')}, now ${fingerprint(changed.jkt)}). Confirm with them before `
        + 'sending a secret — nothing was sent.')
      setPhase('idle')
      return
    }
    if (!a.reachesAMachine && newEndorsers(a).length) {
      setPhase('trust')
      return
    }
    if (!a.reachesAMachine) {
      setPhase('confirm')
      return
    }
    await seal(a)
  }, [seal])

  const start = async () => {
    if (!value) return
    setError('')
    setPhase('checking')
    try {
      // One passkey touch both steps up and opens the person's own key — the
      // anchor no server can substitute. No passkey key: pins decide.
      let self = me
      if (!self) {
        const ks = await getKeyset().catch(() => null)
        self = ks?.exists ? await stepUpAndUnlock(ks).catch(() => null) : null
        setMe(self)
      }
      const d = await getZkRecipients(slug, requestId)
      setData(d)
      await decide(d, self)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not check who can read this agent')
      setPhase('idle')
    }
  }

  const trust = async (people: Endorser[]) => {
    savePins(people.map((p) => ({ by: p.by, jkt: p.jkt, email: p.email })))
    if (data) await decide(data, me, new Set(people.map((p) => p.by)))
  }

  const unlockOwn = async () => {
    try {
      const self = await unlockVaultKey(unlocker.prompt)
      setMe(self)
      if (data) await decide(data, self)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not unlock your vault key')
    }
  }

  /** An admin vouching for machines nobody has confirmed yet. */
  const confirmMachines = async () => {
    if (!me || !data || !verdict) return
    try {
      const machines = verdict.unconfirmed.filter((r) => r.kind === 'device')
      await endorseReaders(slug, data.agent_id, me, [
        { kind: 'member', target: me.userId, publicJwk: me.publicJwk },
        ...machines.map((r) => ({ kind: r.kind, target: r.target, publicJwk: r.public_jwk as { x?: string; y?: string } })),
      ])
      const d = await getZkRecipients(slug, requestId)
      setData(d)
      await decide(d, me)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not confirm these machines')
    }
  }

  if (phase === 'done' || req?.status === 'fulfilled') {
    return (
      <div className="my-2 flex items-center gap-2 rounded-lg border border-success/40 bg-success/10 px-3 py-2 text-[12px]"
           data-testid="secret-seal-done">
        <ShieldCheck className="h-4 w-4 text-success" />
        <span>
          <span className="font-mono">{name}</span> is stored for <b>{slug}</b>
          {sealedTo ? ` — sealed in your browser to ${sealedTo} reader${sealedTo === 1 ? '' : 's'}` : ''}
          {req?.fulfilled_by_email && !sealedTo ? ` by ${req.fulfilled_by_email}` : ''}. Nobody else saw it.
        </span>
      </div>
    )
  }

  return (
    <div className="my-2 space-y-2 rounded-lg border border-border bg-card p-3 text-[12px]" data-testid="secret-seal-card">
      <div className="flex items-start gap-2">
        <KeyRound className="mt-0.5 h-4 w-4 shrink-0 text-primary" />
        <div className="min-w-0">
          <div className="text-foreground">
            {requestId ? <><b>{slug}</b> asks for </> : <>Add to <b>{slug}</b>&apos;s vault: </>}
            <span className="font-mono">{name}</span>
          </div>
          {req?.reason && <div className="text-muted-foreground">“{req.reason}”</div>}
          <div className="text-[11px] text-muted-foreground">
            Sealed in this browser to the machines and people who may read {slug}. The agent never sees it, and
            neither does canopy.
          </div>
        </div>
      </div>

      {closed ? (
        <div className="text-muted-foreground" data-testid="secret-seal-closed">This request is {req?.status}.</div>
      ) : (
        // Not a <form>: the card is also mounted inside other forms (Share secret).
        <div className="flex flex-wrap items-center gap-2" role="group" aria-label={`Seal ${name}`}>
          <input
            type="password"
            autoComplete="off"
            aria-label={`Value for ${name}`}
            data-testid="secret-seal-value"
            value={value}
            onChange={(e) => setValue(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') {
                e.preventDefault()
                void start()
              }
            }}
            disabled={phase !== 'idle'}
            placeholder={`Paste ${name}`}
            className="min-h-11 w-full flex-1 rounded-md border border-input bg-input px-2 py-1 font-mono text-[12px] text-foreground placeholder:text-muted-foreground sm:min-h-0 sm:w-64"
          />
          <button
            type="button"
            onClick={() => void start()}
            data-testid="secret-seal-submit"
            disabled={!value || phase !== 'idle'}
            className="min-h-11 rounded-md bg-primary px-3 py-1 text-[12px] font-medium text-primary-foreground disabled:opacity-40 sm:min-h-0"
          >
            {phase === 'checking' || phase === 'sealing'
              ? <Loader2 className="inline h-3 w-3 animate-spin" />
              : 'Seal and store'}
          </button>
        </div>
      )}

      {phase === 'trust' && verdict && (
        <div className="space-y-2 rounded-md border border-warning/40 bg-warning/10 p-2" data-testid="secret-seal-trust">
          <div>This browser has not trusted these people&apos;s vault keys before. They vouch for who can read
            {' '}{slug}. Check each fingerprint with them (Settings → Vault key shows theirs) before you trust it:</div>
          <ul className="space-y-1">
            {newEndorsers(verdict).map((p) => (
              <li key={p.by}>
                <b>{p.email || `user ${p.by}`}</b> · <span className="font-mono">{fingerprint(p.jkt)}</span>
                <span className="text-muted-foreground"> — vouches for {p.covers.length} reader{p.covers.length === 1 ? '' : 's'}</span>
              </li>
            ))}
          </ul>
          <div className="flex flex-wrap gap-2">
            <button type="button" data-testid="secret-seal-trust-ok"
                    onClick={() => void trust(newEndorsers(verdict))}
                    className="rounded-md bg-primary px-3 py-1 text-[12px] font-medium text-primary-foreground">
              I trust these keys — seal
            </button>
            {!me && (
              <button type="button" onClick={() => void unlockOwn()}
                      className="rounded-md border border-input px-3 py-1 text-[12px]">
                Check against my own vault key instead
              </button>
            )}
            <button type="button" onClick={() => { setPhase('idle') }}
                    className="rounded-md border border-input px-3 py-1 text-[12px]">Cancel</button>
          </div>
        </div>
      )}

      {phase === 'confirm' && verdict && (
        <div className="space-y-2 rounded-md border border-warning/40 bg-warning/10 p-2" data-testid="secret-seal-confirm">
          <div className="flex items-center gap-1"><TriangleAlert className="h-3.5 w-3.5 text-warning" />
            No machine that runs {slug} has been confirmed by anyone you trust.</div>
          {verdict.unconfirmed.filter((r) => r.kind === 'device').length > 0 && me ? (
            <>
              <div>If these are {slug}&apos;s machines, confirm them with your vault key:</div>
              <ul className="space-y-1">
                {verdict.unconfirmed.filter((r) => r.kind === 'device').map((r) => (
                  <li key={r.target}>{r.label} · <span className="font-mono">{fingerprint(r.key_jkt)}</span></li>
                ))}
              </ul>
              <button type="button" data-testid="secret-seal-confirm-ok" onClick={() => void confirmMachines()}
                      className="rounded-md bg-primary px-3 py-1 text-[12px] font-medium text-primary-foreground">
                These are {slug}&apos;s — confirm and seal
              </button>
            </>
          ) : (
            <div className="text-muted-foreground">Ask {slug}&apos;s owner to open its Credentials page once to confirm them.</div>
          )}
          {verdict.sealTo.length > 0 && (
            <button type="button" onClick={() => void seal(verdict)}
                    className="ml-2 rounded-md border border-input px-3 py-1 text-[12px]">
              Seal for its admins only (a machine gets it when one opens the vault)
            </button>
          )}
          <button type="button" onClick={() => setPhase('idle')}
                  className="ml-2 rounded-md border border-input px-3 py-1 text-[12px]">Cancel</button>
        </div>
      )}

      {unlocker.element}
      {error && <div className="text-destructive" role="alert" data-testid="secret-seal-error">{error}</div>}
    </div>
  )
}
