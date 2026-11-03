import { useState, type JSX } from 'react'
import {
  getRunnerCredentialStatus,
  listRunnerCredentialSets,
  setRunnerCredentialSet,
  shareRunnerCredentials,
  type CredentialStatus,
  type RunnerCredentialSet,
} from '@/api/harness'
import { smallButton } from '@/components/supervisor/CredentialKeyForms'

/** What a set's list holds, in order: each entry's name, or its kind when it has none. */
function entriesLine(cset: RunnerCredentialSet): string {
  const kinds: Record<string, string> = { login: 'login', token: 'token', api_key: 'API key' }
  const entries = cset.entries ?? []
  if (!entries.length) return 'nothing yet'
  return entries.map((e) => (e.name ? `${e.name} (${kinds[e.kind] ?? e.kind})` : kinds[e.kind] ?? e.kind)).join(', ')
}

/** Whose list this is. A credential set is one list several of your cloud runners read:
 *  making one (from this runner's own list) is separate from choosing which runners read
 *  it. A runner on a set reads the set's list — in any workspace tree — and its VALUES
 *  from its own tree's vault: a vault never serves two trees, so each tree's vault holds
 *  its own copy of each value. A runner reading its own list can become a set, or read one. */
export function CredentialSetRow({ runnerId, status, busy, write }: {
  runnerId: string
  status: CredentialStatus
  busy: boolean
  write: (fn: () => Promise<CredentialStatus>, fallback: string) => Promise<boolean>
}): JSX.Element {
  const [sets, setSets] = useState<RunnerCredentialSet[] | null>(null)
  const cset = status.credential_set ?? null

  if (cset) {
    const others = cset.runners.filter((r) => r.id !== runnerId)
    const mine = new Set((status.vaults ?? []).map((v) => v.vault_id))
    const here = cset.vaults.find((v) => mine.has(v.vault_id))
    const elsewhere = cset.vaults.filter((v) => !mine.has(v.vault_id))
    const candidates = cset.candidates ?? []
    const leave = () => {
      if (!window.confirm(`Take this runner off ${cset.name}? It goes back to a list of its own, empty until you add to it.`)) return
      return write(() => setRunnerCredentialSet(runnerId, null), 'Failed to leave the set')
    }
    const add = (id: string) => write(async () => {
      await setRunnerCredentialSet(id, cset.id)
      return getRunnerCredentialStatus(runnerId)
    }, 'Failed to add the runner')
    return (
      <div className="flex flex-col gap-1 rounded-md border border-border p-2 text-[12px]" data-testid="cred-set">
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-foreground">Credential set: <span className="font-medium">{cset.name}</span></span>
          <button type="button" onClick={() => void leave()} disabled={busy} data-testid="cred-set-leave"
            className={`ml-auto ${smallButton}`}>Take this runner off it</button>
        </div>
        <span className="text-[11px] text-foreground-secondary" data-testid="cred-set-entries">
          Its list: {entriesLine(cset)}
        </span>
        <span className="text-[11px] text-foreground-secondary" data-testid="cred-set-others">
          Read by this runner{others.length ? `, ${others.map((r) => r.name).join(', ')}` : ' only, so far'}
        </span>
        {candidates.length > 0 && (
          <div className="flex flex-wrap items-center gap-2">
            <select value="" disabled={busy} data-testid="cred-set-add"
              onChange={(ev) => { if (ev.target.value) void add(ev.target.value) }}
              className="rounded-md border border-input bg-input px-1.5 py-0.5 text-[12px] text-foreground">
              <option value="" disabled>Add a runner…</option>
              {candidates.map((c) => (
                <option key={c.id} value={c.id} disabled={!!c.refusal}>
                  {c.name}{c.refusal ? ` — ${c.refusal}` : c.new_tree ? ` — ${c.root}: its values are added there` : ''}
                </option>
              ))}
            </select>
            <span className="text-[11px] text-foreground-subtle">your cloud runners; a runner on the set reads this list instead of its own</span>
          </div>
        )}
        <span className="text-[11px] text-foreground-subtle">
          Changes to the list reach every runner on it. Values are kept per workspace tree: this runner reads
          {here ? ` ${here.name}` : ' its tree’s vault'}.
        </span>
        {here && here.missing.length > 0 && (
          <span className="text-[11px] text-warning" data-testid="cred-set-missing">
            {here.missing.length === 1 ? '1 entry has' : `${here.missing.length} entries have`} no value in
            this tree yet: get a token or add the key below.
          </span>
        )}
        {elsewhere.map((v) => (
          <span key={v.vault_id} className="text-[11px] text-foreground-subtle" data-testid={`cred-set-tree-${v.root}`}>
            {v.root}: {v.kv === 0 ? 'no key yet' : v.missing.length ? `${v.missing.length} missing` : 'every value set'}
          </span>
        ))}
      </div>
    )
  }

  const make = () => {
    const name = window.prompt('Name the credential set. This runner\'s list moves into it; you then choose which other runners read it.')?.trim()
    if (!name) return
    return write(async () => {
      await shareRunnerCredentials(runnerId, name)
      return getRunnerCredentialStatus(runnerId)
    }, 'Failed to make the credential set')
  }
  const choose = (id: string) => write(() => setRunnerCredentialSet(runnerId, id), 'Failed to use the set')
  const open = async () => {
    try {
      setSets(await listRunnerCredentialSets())
    } catch {
      setSets([])
    }
  }
  return (
    <div className="flex flex-col gap-1 text-[12px]" data-testid="cred-set-none">
      <div className="flex flex-wrap items-center gap-2">
        <button type="button" onClick={() => void make()} disabled={busy} data-testid="cred-set-share" className={smallButton}>
          Make a credential set from this list
        </button>
        {sets === null ? (
          <button type="button" onClick={() => void open()} disabled={busy} data-testid="cred-set-use" className={smallButton}>
            Use a credential set…
          </button>
        ) : sets.length === 0 ? (
          <span className="text-[11px] text-foreground-subtle">You have no credential sets yet.</span>
        ) : (
          <select defaultValue="" disabled={busy} data-testid="cred-set-pick"
            onChange={(ev) => { if (ev.target.value) void choose(ev.target.value) }}
            className="rounded-md border border-input bg-input px-1.5 py-0.5 text-[12px] text-foreground">
            <option value="" disabled>Choose a credential set</option>
            {sets.map((c) => (
              <option key={c.id} value={c.id}>
                {c.name}: {entriesLine(c)} · {c.runners.length} runner{c.runners.length === 1 ? '' : 's'}
              </option>
            ))}
          </select>
        )}
      </div>
      <span className="text-[11px] text-foreground-subtle">
        A credential set is one list several of your cloud runners read: add, rename or remove an entry once and every runner on it has the change.
      </span>
    </div>
  )
}
