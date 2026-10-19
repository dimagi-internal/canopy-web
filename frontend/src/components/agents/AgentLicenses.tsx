import { useCallback, useEffect, useState } from 'react'

import {
  approveAgentLicense,
  listAgentLicenses,
  revokeAgentLicense,
  type AgentLicenseOut,
} from '@/api/agents'
import { relativeAge } from '@/lib/relativeAge'

// WHO USES THIS AGENT LOCALLY (canopy-web#1704).
//
// A license is a person running the agent in their own Claude Code, on their own
// private runner, with canopy keeping the record. The caller list decides who MAY
// (the People and Callers settings); this table is who DOES — since when, from
// which machines, how often, and how many credential reads went out under it.
// Admins revoke per person here without editing the rule; approve re-asks the
// caller list on the server, so it can never license someone the list does not
// give the whole agent.

const STATUS_LABEL: Record<AgentLicenseOut['status'], string> = {
  active: 'Active',
  requested: 'Requested',
  revoked: 'Revoked',
}

const STATUS_CLASS: Record<AgentLicenseOut['status'], string> = {
  active: 'text-success',
  requested: 'text-warning',
  revoked: 'text-muted-foreground',
}

export function AgentLicenses({ agentSlug, canEdit }: { agentSlug: string; canEdit: boolean }) {
  const [rows, setRows] = useState<AgentLicenseOut[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState<number | null>(null)

  const load = useCallback(async () => {
    try {
      setRows(await listAgentLicenses(agentSlug))
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Could not load who uses it locally')
    }
  }, [agentSlug])

  useEffect(() => {
    void load()
  }, [load])

  const act = async (userId: number, fn: (slug: string, id: number) => Promise<unknown>) => {
    setBusy(userId)
    setError(null)
    try {
      await fn(agentSlug, userId)
      await load()
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Could not change the license')
    } finally {
      setBusy(null)
    }
  }

  if (rows === null) {
    return error ? (
      <p role="alert" className="m-0 text-[12px] text-destructive">
        {error}
      </p>
    ) : (
      <div className="h-16 animate-pulse rounded-lg bg-muted" />
    )
  }

  return (
    <div className="space-y-2">
      {error && (
        <p role="alert" className="m-0 text-[12px] text-destructive">
          {error}
        </p>
      )}
      {rows.length === 0 ? (
        <p className="m-0 text-[12px] text-muted-foreground">
          Nobody uses it locally yet. A person registers their machine with{' '}
          <code>canopy private-runner register</code> and opens Claude Code with{' '}
          <code>CANOPY_AGENT={agentSlug}</code>; their first session asks for a license.
        </p>
      ) : (
        <table className="w-full text-[12px]">
          <thead className="text-left text-muted-foreground">
            <tr>
              <th className="py-1 pr-3 font-medium">Person</th>
              <th className="py-1 pr-3 font-medium">License</th>
              <th className="py-1 pr-3 font-medium">Why</th>
              <th className="py-1 pr-3 font-medium">Private runners</th>
              <th className="py-1 pr-3 font-medium">Last used</th>
              <th className="py-1 pr-3 text-right font-medium">Turns</th>
              <th className="py-1 pr-3 text-right font-medium">Reads</th>
              {canEdit && <th className="py-1" />}
            </tr>
          </thead>
          <tbody className="divide-y divide-border">
            {rows.map((r) => (
              <tr key={r.user_id}>
                <td className="py-1.5 pr-3" title={r.email}>
                  {r.name}
                </td>
                <td className={`py-1.5 pr-3 ${STATUS_CLASS[r.status]}`} title={r.note || undefined}>
                  {STATUS_LABEL[r.status]}
                </td>
                <td className="py-1.5 pr-3 text-muted-foreground">{r.basis || r.note || '—'}</td>
                <td className="py-1.5 pr-3">{r.private_runners?.join(', ') || '—'}</td>
                <td className="py-1.5 pr-3 text-muted-foreground">
                  {r.last_used_at ? relativeAge(r.last_used_at) : '—'}
                </td>
                <td className="py-1.5 pr-3 text-right tabular-nums">{r.local_turns ?? 0}</td>
                <td className="py-1.5 pr-3 text-right tabular-nums">{r.credential_reads ?? 0}</td>
                {canEdit && (
                  <td className="py-1.5 text-right">
                    {r.status === 'active' ? (
                      <button
                        type="button"
                        className="text-destructive hover:underline disabled:opacity-50"
                        disabled={busy === r.user_id}
                        onClick={() => void act(r.user_id, revokeAgentLicense)}
                      >
                        Revoke
                      </button>
                    ) : (
                      <button
                        type="button"
                        className="text-primary hover:underline disabled:opacity-50"
                        disabled={busy === r.user_id}
                        onClick={() => void act(r.user_id, approveAgentLicense)}
                      >
                        Approve
                      </button>
                    )}
                  </td>
                )}
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  )
}
