import { useCallback, useEffect, useState } from 'react'
import { WorkbenchSkeleton } from 'canopy-ui'

import {
  AgentApiError,
  deleteAgentCredential,
  getAgentCredentialStatus,
  setAgentCredentials,
  type AgentCredentialStatusOut,
} from '@/api/agents'
import { getCredentialAlerts, type AgentCredentialAlertOut } from '@/api/deviceKeys'
import { getAgentMailbox } from '@/api/mail'
import type { RunnerDeviceKeyOut } from '@/api/deviceKeys'
import type { VaultMembersOut, ZkStateOut } from '@/api/vault'
import { EncryptedBadge, VaultEditor, type VaultEntry } from '@/components/vault/VaultEditor'
import { LoadFailed } from '@/components/vault/LoadFailed'
import { VaultSection } from '@/components/vault/VaultSection'
import { VaultView } from '@/components/vault/VaultView'
import { AgentCredentialAlertsSection, groupAlerts, isExpected } from '@/pages/agents/AgentCredentialAlertsSection'
import { AgentDeviceKeysSection } from '@/pages/agents/AgentDeviceKeysSection'
import { AgentVaultMembersSection } from '@/pages/agents/AgentVaultMembersSection'
import { AgentZkSection } from '@/pages/agents/AgentZkSection'
import { CredentialReadHistory } from '@/pages/agents/CredentialReadHistory'
import { OpenSecretRequests } from '@/pages/agents/OpenSecretRequests'
import { sections } from '@/pages/agents/agentCredentials'
import { GOG_TOKEN_REF } from '@/pages/agents/googleMint'
import { revealZkValue, setZkValue, unlockVaultKey } from '@/vault/operations'
import { isPasskeyRefusal, PASSKEY_NOT_COMPLETED, PrfUnsupportedError, PRF_UNSUPPORTED } from '@/vault/prf'
import { useUnlockPrompt } from '@/vault/UnlockPrompt'

// The agent's vault, drawn by the shared `VaultView` (the same frame a workspace's
// vault uses): a summary line, then Secrets (one table you change in place,
// `VaultEditor`), Who can open them (people and machines) and Storage & key. This
// file is the agent's ADAPTER: it reads the agent's rows, key state, members and
// machines and hands the frame each section's body and one-line summary.
//
// The status route returns booleans and timestamps and never plaintext. The
// agent vault is zero-knowledge (canopy-web#1550): Reveal opens a value HERE
// with the person's own vault key, and canopy-web never holds one it can read.
// Save seals in the browser once the agent has a vault key; before that it
// hands the value to the server, which seals it to the agent's readers and
// keeps no copy.

/** The server's own reason (`detail`) when there is one, not the wrapper text. */
function reason(e: unknown, fallback: string): string {
  if (e instanceof AgentApiError && e.detail) return e.detail
  if (e instanceof PrfUnsupportedError) return PRF_UNSUPPORTED
  if (isPasskeyRefusal(e)) return PASSKEY_NOT_COMPLETED
  return e instanceof Error ? e.message : fallback
}

export function AgentSecretsPanel({
  agent,
  canEdit = true,
  reloadKey = 0,
}: {
  agent: {
    slug: string
    name?: string
    email?: string | null
  }
  canEdit?: boolean
  /** Bumped by the page when something outside changed a secret (a new canopy login). */
  reloadKey?: number
}) {
  const [rows, setRows] = useState<AgentCredentialStatusOut[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  // The secrets could not be read at all: say so, not 'no secrets yet'.
  const [loadFailed, setLoadFailed] = useState(false)
  const [zk, setZk] = useState<ZkStateOut | null>(null)
  const [alerts, setAlerts] = useState<AgentCredentialAlertOut[] | null>(null)
  const [members, setMembers] = useState<VaultMembersOut | null>(null)
  const [machines, setMachines] = useState<RunnerDeviceKeyOut[] | null>(null)
  // A canopy-hosted address has no Google mailbox, so it implies no gog-token.
  const [hosted, setHosted] = useState(false)
  const unlocker = useUnlockPrompt()
  const unlock = () => unlockVaultKey(unlocker.prompt)

  const reload = useCallback(() => {
    getAgentCredentialStatus(agent.slug)
      .then(setRows)
      .catch(() => setError('Could not refresh the list of secrets — reload the page.'))
  }, [agent.slug])

  useEffect(() => {
    let cancelled = false
    setRows(null)
    getAgentCredentialStatus(agent.slug)
      .then((r) => { if (!cancelled) { setError(null); setRows(r) } })
      .catch((e: unknown) => {
        if (cancelled) return
        setError(e instanceof Error ? e.message : 'Failed to load')
        setLoadFailed(true)
        setRows([])
      })
    return () => {
      cancelled = true
    }
  }, [agent.slug, reloadKey])

  useEffect(() => {
    let cancelled = false
    if (!canEdit) return
    getCredentialAlerts(agent.slug).then((a) => !cancelled && setAlerts(a), () => !cancelled && setAlerts([]))
    return () => {
      cancelled = true
    }
  }, [agent.slug, canEdit])

  useEffect(() => {
    if (!agent.email?.trim()) return
    // If we cannot tell, assume a hosted address: better to imply no gog-token than to flag one missing.
    getAgentMailbox(agent.slug).then((m) => setHosted(m.kind === 'hosted'), () => setHosted(true))
  }, [agent.slug, agent.email])

  if (rows === null) return <WorkbenchSkeleton />

  const implied = agent.email?.trim() && !hosted ? [GOG_TOKEN_REF] : []
  const sec = sections(rows, implied)
  const orphan = new Set(sec.orphans.map((r) => r.name))
  const entries: VaultEntry[] = rows.map((r) => ({
    name: r.name,
    set: r.set,
    updatedAt: r.updated_at,
    updatedBy: r.updated_by_email,
    lastReadAt: r.last_read_at,
    readCount: r.read_count,
    sealed: r.account === 'delegated',
    flag: r.account === 'delegated'
      ? { label: r.account_label || 'delegated', tone: 'neutral' }
      : orphan.has(r.name)
      ? { label: 'not declared', tone: 'destructive' }
      : r.set
        ? undefined
        : { label: 'no value', tone: 'warning' },
  }))
  const missing = sec.notSet.length
  const byName = new Map(rows.map((r) => [r.name, r]))

  const save = async (name: string, value: string) => {
    if (zk) {
      await setZkValue(agent.slug, name, value, unlock)
      setRows(await getAgentCredentialStatus(agent.slug))
    } else {
      setRows(await setAgentCredentials(agent.slug, { [name]: value }))
    }
  }
  const reveal = (name: string) => revealZkValue(agent.slug, name, unlock)
  const remove = async (name: string) => setRows(await deleteAgentCredential(agent.slug, name))

  const stored = rows.filter((r) => r.set).length
  const waiting = zk?.pending.length ?? 0
  const rotate = Boolean(zk?.rotation_recommended)
  const secretsAttention = sec.orphans.length > 0 || missing > 0
  const activeMachines = machines?.filter((k) => k.status === 'active').length
  const alertGroups = groupAlerts(alerts ?? [])
  const unexpectedAlerts = alertGroups.filter((g) => !isExpected(g.kind)).length
  const attention = [
    secretsAttention && [
      sec.orphans.length > 0 && `${sec.orphans.length} stored secret${sec.orphans.length === 1 ? '' : 's'} nothing declares`,
      missing > 0 && `${missing} declared secret${missing === 1 ? '' : 's'} with no value`,
    ].filter(Boolean).join(' · '),
    waiting > 0 && `${waiting} waiting to be given access`,
    rotate && 'someone no longer allowed could still open it — change the key',
    unexpectedAlerts > 0 && `${unexpectedAlerts} unusual read${unexpectedAlerts === 1 ? '' : 's'}`,
  ].filter((a): a is string => Boolean(a))

  return (
    <div data-testid="agent-secrets">
      <VaultView
        facts={{
          secrets: loadFailed ? undefined : stored,
          people: canEdit ? members?.members.length : undefined,
          machines: canEdit ? activeMachines : undefined,
          attention: canEdit ? attention : [],
        }}
        secrets={{
          summary: secretsAttention
            ? [`${stored} stored`, missing > 0 && `${missing} need a value`, sec.orphans.length > 0 && `${sec.orphans.length} undeclared`]
                .filter(Boolean).join(' · ')
            : `${stored} stored`,
          tone: secretsAttention ? 'attention' : 'ok',
          defaultOpen: true,
          anchors: ['credentials'],
          body: (
            <>
              {canEdit && zk && <OpenSecretRequests slug={agent.slug} onStored={reload} />}

              {/* Whether this tab wants anything from the reader. */}
              {secretsAttention && (
                <p className="mb-2 text-[12px] text-warning" data-testid="agent-credentials-summary">
                  {[
                    sec.orphans.length > 0 && `${sec.orphans.length} stored secret(s) nothing declares any more — worth removing.`,
                    missing > 0 && `${missing} declared secret(s) have no value yet.`,
                  ].filter(Boolean).join(' ')}
                </p>
              )}
              {loadFailed ? <LoadFailed what="its secrets" /> : <VaultEditor
                entries={entries}
                canEdit={canEdit}
                onSave={save}
                onReveal={reveal}
                onDelete={remove}
                describeError={reason}
                badge={zk ? <EncryptedBadge /> : undefined}
                details={(e) => {
                  const row = byName.get(e.name)
                  return row?.set && canEdit ? <CredentialReadHistory slug={agent.slug} row={row} alerts={alerts} /> : null
                }}
              />}
              {error && (
                <p className="mt-3 text-[13px] text-destructive" data-testid="agent-credentials-error">
                  {error}
                </p>
              )}
              <p className="mt-4 text-[11px] text-muted-foreground" data-testid="credentials-note">
                Every value here is encrypted so that canopy-web cannot read it: only this agent&rsquo;s machines and the
                people who manage its vault can open one. Saving or revealing one from the browser asks for your passkey.
                {!canEdit && <> Only {agent.name ?? agent.slug}&rsquo;s admins can change or reveal them.</>}
              </p>
            </>
          ),
        }}
        access={{
          summary: canEdit
            ? [members && `${members.members.length} ${members.members.length === 1 ? 'person' : 'people'}`,
               activeMachines !== undefined && `${activeMachines} ${activeMachines === 1 ? 'machine' : 'machines'}`]
                .filter(Boolean).join(' · ') || undefined
            : 'admins only',
          anchors: ['cred-vault-members', 'cred-device-keys'],
          body: (
            <>
              <AgentVaultMembersSection slug={agent.slug} onLoaded={setMembers} canEdit={canEdit} />
              <AgentDeviceKeysSection
                slug={agent.slug}
                canEdit={canEdit}
                onLoaded={setMachines}
              />
              {!canEdit && (
                <p className="text-[12px] text-muted-foreground">
                  Only {agent.name ?? agent.slug}&rsquo;s admins can see who and what can open its secrets.
                </p>
              )}
            </>
          ),
        }}
        storage={{
          summary: zk && zk.kv > 0 ? 'key and recovery' : 'key not made yet',
          tone: canEdit && (waiting > 0 || rotate) ? 'attention' : 'quiet',
          anchors: ['cred-zk', 'cred-vault', 'secrets-home'],
          defaultOpen: false,
          body: (
            <>
              <AgentZkSection slug={agent.slug} canEdit={canEdit} onState={setZk} onChanged={reload} />
            </>
          ),
        }}
      >
        {canEdit && alerts && alerts.length > 0 && (
          <VaultSection id="read-alerts" title="Read alerts" tone={unexpectedAlerts > 0 ? 'attention' : 'quiet'}
            anchors={['cred-alerts']}
            summary={unexpectedAlerts > 0
              ? `${unexpectedAlerts} to look at${alertGroups.length > unexpectedAlerts ? `, ${alertGroups.length - unexpectedAlerts} expected` : ''}`
              : `${alertGroups.length} asked-for secret${alertGroups.length === 1 ? '' : 's'} no skill lists`}>
            <AgentCredentialAlertsSection alerts={alerts} />
          </VaultSection>
        )}
      </VaultView>
      {unlocker.element}
    </div>
  )
}
