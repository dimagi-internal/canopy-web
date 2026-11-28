import { useEffect, useState } from 'react'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from 'canopy-ui/ui'

import { listMembersLearningMode, setMemberLearningMode, type MemberLearningMode } from '@/api/learningMode'

/** The members page, for owners: who is in learning mode here, and a switch to turn it on or off for them (a
 *  parent for their kids). It acts only in the workspaces of theirs you own; the person is told. */
export function MemberLearningModeSection({ slug }: { slug: string }) {
  const [rows, setRows] = useState<MemberLearningMode[] | null>(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState<number | null>(null)

  useEffect(() => {
    listMembersLearningMode(slug)
      .then(setRows)
      .catch((e: unknown) => setError(e instanceof Error ? e.message : 'Could not load learning mode'))
  }, [slug])

  const toggle = async (row: MemberLearningMode, on: boolean) => {
    setBusy(row.user_id)
    setError('')
    try {
      const res = await setMemberLearningMode(slug, row.user_id, on)
      setRows((rs) => (rs ?? []).map((r) => (r.user_id === row.user_id ? res.member : r)))
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not change')
    } finally {
      setBusy(null)
    }
  }

  return (
    <div className="rounded-lg border border-border bg-card p-5" data-testid="member-learning-mode">
      <h2 className="mb-1 text-sm font-semibold text-foreground">Learning mode</h2>
      <p className="mb-3 text-[12px] text-muted-foreground">
        A person in learning mode lets coaches, this workspace&apos;s owners and admins, and the admins of the agents
        they talk to read their sessions, read-only, never a sensitive one. You may turn it on or off for someone here;
        they are told and can change it themselves.
      </p>
      {error && <p className="mb-2 text-[12px] text-destructive">{error}</p>}
      {rows === null ? (
        <div className="h-16 animate-pulse rounded-lg bg-muted" />
      ) : (
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Person</TableHead>
              <TableHead>Learning mode</TableHead>
              <TableHead>Turned on by</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {rows.map((r) => (
              <TableRow key={r.user_id}>
                <TableCell className="whitespace-normal text-foreground">{r.name || r.email}</TableCell>
                <TableCell>
                  <label className="flex items-center gap-2 text-[13px]">
                    <input
                      type="checkbox"
                      checked={r.on}
                      disabled={busy === r.user_id}
                      aria-label={`Learning mode for ${r.email}`}
                      onChange={(e) => void toggle(r, e.target.checked)}
                    />
                    {r.on ? `On${r.workspaces.length ? ` (${r.workspaces.join(', ')})` : ''}` : 'Off'}
                  </label>
                </TableCell>
                <TableCell className="whitespace-normal text-muted-foreground">
                  {r.on ? `${r.set_by || '—'}${r.set_at ? ` · ${new Date(r.set_at).toLocaleDateString()}` : ''}` : ''}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}
    </div>
  )
}
