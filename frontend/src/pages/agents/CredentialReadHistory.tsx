import { useState } from 'react'
import type { AgentCredentialStatusOut } from '@/api/agents'
import { getCredentialReads, type AgentCredentialReadOut } from '@/api/deviceKeys'
import { relativeAge } from '@/lib/relativeAge'

// The read log for one credential (Canopy Vault, canopy-web#1538): when its
// value was last handed out, and — on demand — by whom, to which box, for which
// turn. Never the value.

export function CredentialReadHistory({ slug, row }: { slug: string; row: AgentCredentialStatusOut }) {
  const [reads, setReads] = useState<AgentCredentialReadOut[] | null>(null)
  const [open, setOpen] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const toggle = async () => {
    const next = !open
    setOpen(next)
    if (next && reads === null) {
      try {
        setReads(await getCredentialReads(slug, row.name))
      } catch (e) {
        setError(e instanceof Error ? e.message : 'Failed to load')
        setReads([])
      }
    }
  }

  return (
    <>
      <button
        type="button"
        onClick={() => void toggle()}
        className="text-[11px] text-muted-foreground underline-offset-2 hover:underline"
        aria-expanded={open}
        data-testid={`cred-reads-${row.name}`}
      >
        {row.last_read_at ? `read ${relativeAge(row.last_read_at)} · ${row.read_count}×` : 'never read'}
      </button>
      {open && (
        <div className="basis-full" data-testid={`cred-reads-list-${row.name}`}>
          {reads === null ? (
            <p className="text-[11px] text-muted-foreground">…</p>
          ) : reads.length === 0 ? (
            <p className="text-[11px] text-muted-foreground">{error ?? 'No reads recorded.'}</p>
          ) : (
            <table className="mt-1 w-full text-[11px]">
              <tbody className="divide-y divide-border">
                {reads.map((r, i) => (
                  <tr key={`${r.created_at}-${i}`}>
                    <td className="py-1 text-muted-foreground">{relativeAge(r.created_at)}</td>
                    <td className="py-1 text-foreground">{r.user_email || '—'}</td>
                    <td className="py-1 text-foreground">{r.runner_name || '—'}</td>
                    <td className="py-1 font-mono text-muted-foreground">{r.turn_id || ''}</td>
                    <td className="py-1 text-muted-foreground">{r.via}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      )}
    </>
  )
}
