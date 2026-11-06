import { useEffect, useState } from 'react'

import { Td, Th, Tr } from '@/components/DataTable'
import { LoadFailed } from '@/components/vault/LoadFailed'
import type { VaultLogEntryOut } from '@/api/vault'

// A vault's Log: what changed and who read what, newest first, one table. The page that owns
// the vault loads the entries (a workspace vault: GET /api/vaults/{id}/log); this draws them.
// Never a value — names, who, when and from where.

const when = (at: string) => new Date(at).toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' })

export function VaultLog({ load, onLoaded }: {
  load: () => Promise<VaultLogEntryOut[]>
  onLoaded?: (entries: VaultLogEntryOut[]) => void
}) {
  const [entries, setEntries] = useState<VaultLogEntryOut[] | null>(null)
  const [failed, setFailed] = useState(false)
  const [only, setOnly] = useState<'all' | 'change' | 'read'>('all')
  useEffect(() => {
    let live = true
    load().then((e) => { if (live) { setEntries(e); onLoaded?.(e) } }).catch(() => { if (live) setFailed(true) })
    return () => { live = false }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])
  if (failed) return <LoadFailed what="its log" />
  if (entries === null) return <p className="text-[12px] text-muted-foreground">Loading…</p>
  if (entries.length === 0) return <p className="text-[12px] text-muted-foreground">Nothing yet.</p>
  const shown = only === 'all' ? entries : entries.filter((e) => e.kind === only)
  const pick = (k: typeof only, label: string) => (
    <button type="button" onClick={() => setOnly(k)} aria-pressed={only === k} data-testid={`vault-log-only-${k}`}
      className={`rounded-md px-2 py-1 text-[12px] ${only === k ? 'bg-muted text-foreground' : 'text-muted-foreground hover:text-foreground'}`}>
      {label}
    </button>
  )
  return (
    <div data-testid="vault-log">
      <div className="mb-2 flex gap-1">{pick('all', 'Everything')}{pick('change', 'Changes')}{pick('read', 'Reads')}</div>
      <table className="w-full text-[12px]">
        <thead>
          <Tr head="label">
            <Th weight="normal">When</Th><Th weight="normal">Who</Th><Th weight="normal">What</Th><Th weight="normal">From</Th>
          </Tr>
        </thead>
        <tbody className="divide-y divide-border">
          {shown.map((e, i) => (
            <tr key={`${e.at}-${i}`} data-testid="vault-log-row">
              <Td density="cozy" tone="muted">{when(e.at)}</Td>
              <Td density="cozy" tone="foreground">{e.who}</Td>
              <Td density="cozy" tone="foreground">{e.what}</Td>
              <Td density="cozy" tone="muted">{e.where}{e.turn_id ? ` · turn ${e.turn_id.slice(0, 8)}` : ''}</Td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
