import { useState, type JSX } from 'react'
import { Button } from 'canopy-ui/ui'
import { approveWithPasskey, type ApprovalCard } from '@/api/approvals'
import { getSealing, putSealedSession } from '@/api/awsSealed'
import { mintSealedSession, SealedSessionError } from '@/aws/sealedSession'
import type { Endorser } from '@/vault/trust'
import { savePins } from '@/vault/trust'
import { useVaultUnlock } from '@/vault/useVaultUnlock'
import { fingerprint } from '@/vault/zk'

type Step = { agent: string; request: number }

/**
 * Approve a SEALED AWS grant (`agent.aws_role`, delivery sealed): this browser opens the role's
 * key with your vault key, asks AWS for the agent's session itself, seals it to the agent's
 * machines you trust, then approves with a passkey touch made for the request and hands the
 * sealed session over. canopy-web sees only ciphertext. After approving, the same button makes
 * a new session for the grant (`renew`).
 */
export function AwsSealedApprove({ card, step, comment, renew = false, onCard }: {
  card: ApprovalCard
  step: Step
  comment: string
  renew?: boolean
  onCard: (c: ApprovalCard) => void
}): JSX.Element {
  const vault = useVaultUnlock()
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [people, setPeople] = useState<Endorser[] | null>(null)
  const [accept, setAccept] = useState<Set<string>>(new Set())
  const [done, setDone] = useState<string | null>(null)
  const [pending, setPending] = useState<{ valueZk: string; expiresAt: string } | null>(null)
  const [approved, setApproved] = useState(renew)

  const handOver = async (sealed: { valueZk: string; expiresAt: string }) => {
    await putSealedSession(step.agent, step.request, { value_zk: sealed.valueZk, expires_at: sealed.expiresAt })
    setPending(null)
    setDone(`Keys made in this browser and sealed to ${step.agent}'s machines, until `
      + `${new Date(sealed.expiresAt).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })}.`)
  }

  const run = async (trusted = accept) => {
    setBusy(true)
    setError(null)
    try {
      let sealed = pending
      if (!sealed) {
        const me = await vault.unlock()
        const sealing = await getSealing(step.agent, step.request)
        const minted = await mintSealedSession(sealing, me, trusted)
        if (minted.kind === 'trust') {
          setPeople(minted.people)
          return
        }
        setPeople(null)
        sealed = { valueZk: minted.valueZk, expiresAt: minted.expiresAt }
        setPending(sealed)
      }
      if (!approved) {
        onCard(await approveWithPasskey(card.id, comment))
        setApproved(true)
      }
      await handOver(sealed)
    } catch (e) {
      setError(e instanceof SealedSessionError || e instanceof Error ? e.message : 'Something went wrong')
    } finally {
      setBusy(false)
    }
  }

  const trustAndContinue = () => {
    if (!people) return
    savePins(people.map((p) => ({ by: p.by, jkt: p.jkt, email: p.email })))
    const next = new Set([...accept, ...people.map((p) => p.by)])
    setAccept(next)
    void run(next)
  }

  return (
    <div className="space-y-2" data-testid="aws-sealed-approve">
      {!done && (
        <p className="text-[12px] text-muted-foreground">
          Your browser makes {step.agent}&rsquo;s AWS keys with this role&rsquo;s sealed key and seals them to its
          machines: canopy-web never holds a usable key. It takes your passkey to open your vault key
          {renew ? '' : ', and once more to approve'}.
        </p>
      )}
      {people && (
        <div className="rounded-md border border-warning bg-warning/10 p-3 text-[13px]" data-testid="aws-sealed-trust">
          <p className="font-medium text-foreground">Do you trust these people&rsquo;s word for {step.agent}&rsquo;s machines?</p>
          <p className="mt-0.5 text-[12px] text-muted-foreground">
            Only machines someone you trust vouched for get the keys. Check each fingerprint with the person.
          </p>
          <ul className="mt-2 space-y-0.5">
            {people.map((p) => (
              <li key={p.by} className="font-mono text-[12px]">{p.email} · {fingerprint(p.jkt)}</li>
            ))}
          </ul>
          <Button className="mt-2" variant="outline" disabled={busy} onClick={trustAndContinue}
                  data-testid="aws-sealed-trust-accept">
            I checked: trust them and continue
          </Button>
        </div>
      )}
      {error && <p className="text-sm text-destructive" data-testid="aws-sealed-error">{error}</p>}
      {done ? (
        <p className="text-[13px] text-foreground" data-testid="aws-sealed-done">{done}</p>
      ) : (
        <Button disabled={busy || (!card.can_decide && !renew)} onClick={() => void run()} data-testid="aws-sealed-run">
          {pending && approved ? 'Hand the keys over again' : pending ? 'Approve with your passkey'
            : renew ? 'Make new keys for this grant' : 'Approve and make the keys'}
        </Button>
      )}
      {vault.element}
    </div>
  )
}
