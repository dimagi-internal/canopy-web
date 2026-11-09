import { useCallback, useEffect, useState } from 'react'

import {
  approveAgentLicense,
  listAgentLicenses,
  revokeAgentLicense,
  type AgentLicenseOut,
} from '@/api/agents'
import { relativeAge } from '@/lib/relativeAge'
import { toneText } from 'canopy-ui/lib'
import { Td, Th } from '@/components/DataTable'

// WHO USES THIS AGENT DIRECTLY.
//
// A license is a person running the agent in their own Claude Code, on their own
// computer, with canopy keeping the record. The caller list decides who MAY
// (the People and Access settings); this table is who DOES — since when, from
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
  active: toneText('success'),
  requested: toneText('warning'),
  revoked: toneText('neutral'),
}

export function AgentLicenses({ agentSlug, canEdit }: { agentSlug: string; canEdit: boolean }) {
  const [rows, setRows] = useState<AgentLicenseOut[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState<number | null>(null)

  const load = useCallback(async () => {
    try {
      setRows(await listAgentLicenses(agentSlug))
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Could not load who uses it directly')
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
          Nobody uses it directly yet. A person runs <code>canopy agent use {agentSlug}</code> once on
          their computer; their first session asks for a license.
        </p>
      ) : (
        <table className="w-full text-[12px]">
          <thead className="text-left text-muted-foreground">
            <tr>
              <Th density="compact-gap">Person</Th>
              <Th density="compact-gap">License</Th>
              <Th density="compact-gap">Why</Th>
              <Th density="compact-gap">Computers</Th>
              <Th density="compact-gap">Last used</Th>
              <Th density="compact-gap" className="text-right">Turns</Th>
              <Th density="compact-gap" className="text-right">Reads</Th>
              {canEdit && <Th weight="inherit" />}
            </tr>
          </thead>
          <tbody className="divide-y divide-border">
            {rows.map((r) => (
              <tr key={r.user_id}>
                <Td density="cozy-gap" title={r.email}>
                  {r.name}
                </Td>
                <Td density="cozy-gap" className={STATUS_CLASS[r.status]} title={r.note || undefined}>
                  {STATUS_LABEL[r.status]}
                </Td>
                <Td density="cozy-gap" tone="muted">{r.basis || r.note || '—'}</Td>
                <Td density="cozy-gap">{r.private_runners?.join(', ') || '—'}</Td>
                <Td density="cozy-gap" tone="muted">
                  {r.last_used_at ? relativeAge(r.last_used_at) : '—'}
                </Td>
                <Td density="cozy-gap" numeric align="right">{r.local_turns ?? 0}</Td>
                <Td density="cozy-gap" numeric align="right">{r.credential_reads ?? 0}</Td>
                {canEdit && (
                  <Td density="cozy" align="right">
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
                  </Td>
                )}
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  )
}
