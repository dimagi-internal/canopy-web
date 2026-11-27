import { useEffect, useState, type JSX } from 'react'
import { Button } from 'canopy-ui/ui'
import {
  deleteSealedKey, getAgentMachines, listSealedKeyHolders, listSealedRoles, putSealedKey,
  type AwsSealedHolder, type AwsSealedRole,
} from '@/api/awsSealed'
import { machinesToSealTo, SealedSessionError } from '@/aws/sealedSession'
import { Td, Th, Tr } from '@/components/DataTable'
import { savePins, trustPerson, type Endorser } from '@/vault/trust'
import { useVaultUnlock } from '@/vault/useVaultUnlock'
import { fingerprint, jkt, sealValue, type Recipient } from '@/vault/zk'

const KEY_NAME = 'AWS_ACCESS_KEY'
const binding = (role: string, agent: string) => (agent ? `aws-role:${role}@${agent}` : `aws-role:${role}`)

/**
 * Sealed AWS keys (`/aws-keys`, superusers): register each labs role's OWN AWS key, sealed in this
 * browser — to the people who approve grants, or to the machines of an agent standing for the role.
 * canopy-web keeps only ciphertext and the key's id (`apps/agents/aws_sealed.py`). The secret you
 * paste never leaves this page unsealed.
 */
export function AwsSealedKeysPage(): JSX.Element {
  const vault = useVaultUnlock()
  const [roles, setRoles] = useState<AwsSealedRole[] | null>(null)
  const [holders, setHolders] = useState<AwsSealedHolder[]>([])
  const [error, setError] = useState<string | null>(null)
  const [role, setRole] = useState('')
  const [agent, setAgent] = useState('')
  const [akid, setAkid] = useState('')
  const [secret, setSecret] = useState('')
  const [chosen, setChosen] = useState<Set<string>>(new Set())
  const [checked, setChecked] = useState<Set<string>>(new Set())
  const [people, setPeople] = useState<Endorser[] | null>(null)
  const [busy, setBusy] = useState(false)
  const [saved, setSaved] = useState<string | null>(null)

  const load = () => Promise.all([listSealedRoles(), listSealedKeyHolders()])
    .then(([r, h]) => { setRoles(r); setHolders(h) })
    .catch((e: unknown) => setError(e instanceof Error ? e.message : 'Could not load the sealed keys'))
  useEffect(() => { void load() }, [])

  if (error && !roles) return <p className="max-w-2xl text-sm text-destructive">{error}</p>
  if (!roles) return <div className="h-32 max-w-3xl animate-pulse rounded-lg bg-muted" />
  const current = roles.find((r) => r.role === role)

  const seal = async (accept = new Set<string>()) => {
    setBusy(true)
    setError(null)
    setSaved(null)
    try {
      if (!role || !akid.trim() || !secret.trim()) throw new Error('Choose a role and paste the key id and secret.')
      const me = await vault.unlock()
      let to: Recipient[]
      if (agent) {
        const found = await machinesToSealTo(await getAgentMachines(agent), me, accept)
        if (found.length && 'by' in found[0]) {
          setPeople(found as Endorser[])
          return
        }
        to = found as Recipient[]
      } else {
        to = []
        const self = { userId: me.userId, publicJwk: me.publicJwk }
        for (const h of holders.filter((x) => chosen.has(x.user_id) || x.user_id === me.userId)) {
          const v = await trustPerson(h, { self })
          if (v.status === 'changed' || v.status === 'bad') throw new Error(`${h.email}'s vault key is not the one this browser trusts.`)
          if (v.status === 'new' && !checked.has(h.user_id)) throw new Error(`Check ${h.email}'s fingerprint with them, then tick it.`)
          if (v.status === 'new') savePins([{ by: h.user_id, jkt: v.jkt, email: h.email }])
          to.push({ kind: 'member', target: h.user_id, publicJwk: h.public_jwk as Recipient['publicJwk'] })
        }
      }
      setPeople(null)
      const plaintext = JSON.stringify({ AccessKeyId: akid.trim(), SecretAccessKey: secret.trim() })
      const valueZk = await sealValue(plaintext, binding(role, agent), KEY_NAME, 0, to)
      await putSealedKey(role, { access_key_id: akid.trim(), value_zk: valueZk, agent })
      setSecret('')
      setSaved(`${role}${agent ? ` for ${agent}` : ''}: sealed to ${to.length} ${agent ? 'machine' : 'person'}${to.length === 1 ? '' : agent ? 's' : 's'}.`)
      await load()
    } catch (e) {
      setError(e instanceof SealedSessionError || e instanceof Error ? e.message : 'Could not seal the key')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="max-w-4xl space-y-4">
      <section className="rounded-lg border border-border bg-card p-4">
        <h1 className="text-sm font-semibold text-foreground">Sealed AWS keys</h1>
        <p className="mt-1 text-[12px] text-muted-foreground">
          Each labs role&rsquo;s own AWS key, sealed in your browser. canopy-web stores only ciphertext it cannot open:
          approvers&rsquo; browsers make an agent&rsquo;s keys from it on a passkey touch, and a standing agent&rsquo;s machines
          make their own. Deactivate a key in IAM when you replace or delete it here.
        </p>
        <table className="mt-3 w-full text-[13px]" data-testid="aws-sealed-roles">
          <thead><Tr><Th>Role</Th><Th>Approvers&rsquo; key</Th><Th>Standing agents</Th><Th /></Tr></thead>
          <tbody>
            {roles.map((r) => {
              const approvers = r.keys?.find((k) => !k.agent)
              return (
                <Tr rule="top" key={r.role}>
                  <Td density="cozy" tone="secondary"><span className="font-mono">{r.role}</span></Td>
                  <Td density="cozy" tone="muted">
                    {approvers ? <><span className="font-mono">{approvers.access_key_id}</span> · {approvers.holders?.join(', ')}</> : 'not sealed'}
                  </Td>
                  <Td density="cozy" tone="muted">
                    {(r.standing ?? []).map((a) => {
                      const k = r.keys?.find((x) => x.agent === a)
                      return <div key={a}>{a}: {k ? `${k.access_key_id} · ${k.machines} machine${k.machines === 1 ? '' : 's'}` : 'not sealed'}</div>
                    })}
                  </Td>
                  <Td density="cozy" tone="muted">
                    {approvers && (
                      <button type="button" className="text-destructive hover:underline"
                              onClick={() => void deleteSealedKey(r.role).then(load)}>Forget</button>
                    )}
                  </Td>
                </Tr>
              )
            })}
          </tbody>
        </table>
      </section>

      <section className="rounded-lg border border-border bg-card p-4 text-[13px]" aria-label="Seal a key">
        <h2 className="text-sm font-semibold text-foreground">Seal a key</h2>
        <div className="mt-2 grid gap-2 sm:grid-cols-[8rem_1fr]">
          <label htmlFor="aws-role" className="text-muted-foreground">Role</label>
          <select id="aws-role" value={role} onChange={(e) => { setRole(e.target.value); setAgent('') }}
                  className="rounded-md border border-input bg-input px-2 py-1">
            <option value="">Choose…</option>
            {roles.map((r) => <option key={r.role} value={r.role}>{r.role}</option>)}
          </select>
          <label htmlFor="aws-for" className="text-muted-foreground">Sealed to</label>
          <select id="aws-for" value={agent} onChange={(e) => setAgent(e.target.value)}
                  className="rounded-md border border-input bg-input px-2 py-1">
            <option value="">The people who approve grants</option>
            {(current?.standing ?? []).map((a) => <option key={a} value={a}>{a}&rsquo;s machines (standing)</option>)}
          </select>
          <label htmlFor="aws-akid" className="text-muted-foreground">Access key id</label>
          <input id="aws-akid" value={akid} onChange={(e) => setAkid(e.target.value)} autoComplete="off"
                 className="rounded-md border border-input bg-input px-2 py-1 font-mono" />
          <label htmlFor="aws-secret" className="text-muted-foreground">Secret access key</label>
          <input id="aws-secret" type="password" value={secret} onChange={(e) => setSecret(e.target.value)}
                 autoComplete="off" className="rounded-md border border-input bg-input px-2 py-1 font-mono" />
        </div>
        {!agent && (
          <div className="mt-3" data-testid="aws-sealed-holders">
            <p className="text-muted-foreground">Who holds it (you always do). Only canopy superusers with a vault key:</p>
            {holders.map((h) => (
              <HolderRow key={h.user_id} h={h} chosen={chosen.has(h.user_id)} checked={checked.has(h.user_id)}
                         onChoose={(v) => setChosen(toggle(chosen, h.user_id, v))}
                         onCheck={(v) => setChecked(toggle(checked, h.user_id, v))} />
            ))}
          </div>
        )}
        {people && (
          <div className="mt-3 rounded-md border border-warning bg-warning/10 p-3" data-testid="aws-sealed-trust">
            <p className="font-medium">Do you trust these people&rsquo;s word for {agent}&rsquo;s machines?</p>
            <ul className="mt-1">{people.map((p) => <li key={p.by} className="font-mono text-[12px]">{p.email} · {fingerprint(p.jkt)}</li>)}</ul>
            <Button className="mt-2" variant="outline" disabled={busy} onClick={() => {
              savePins(people.map((p) => ({ by: p.by, jkt: p.jkt, email: p.email })))
              void seal(new Set(people.map((p) => p.by)))
            }}>I checked: trust them and seal</Button>
          </div>
        )}
        {error && <p className="mt-2 text-sm text-destructive">{error}</p>}
        {saved && <p className="mt-2 text-success" data-testid="aws-sealed-saved">{saved}</p>}
        <Button className="mt-3" disabled={busy} onClick={() => void seal()} data-testid="aws-sealed-seal">
          Seal and save
        </Button>
      </section>
      {vault.element}
    </div>
  )
}

function toggle(set: Set<string>, id: string, on: boolean): Set<string> {
  const next = new Set(set)
  if (on) next.add(id)
  else next.delete(id)
  return next
}

function HolderRow({ h, chosen, checked, onChoose, onCheck }: {
  h: AwsSealedHolder; chosen: boolean; checked: boolean; onChoose: (v: boolean) => void; onCheck: (v: boolean) => void
}): JSX.Element {
  const [thumb, setThumb] = useState('')
  useEffect(() => { void jkt(h.public_jwk as Recipient['publicJwk']).then(setThumb) }, [h.public_jwk])
  return (
    <div className="mt-1 flex flex-wrap items-center gap-3">
      <label className="flex items-center gap-1.5">
        <input type="checkbox" checked={chosen} onChange={(e) => onChoose(e.target.checked)} /> {h.email}
      </label>
      {chosen && (
        <label className="flex items-center gap-1.5 text-[12px] text-muted-foreground">
          <input type="checkbox" checked={checked} onChange={(e) => onCheck(e.target.checked)} />
          fingerprint checked with them: <span className="font-mono">{thumb ? fingerprint(thumb) : '…'}</span>
        </label>
      )}
    </div>
  )
}
