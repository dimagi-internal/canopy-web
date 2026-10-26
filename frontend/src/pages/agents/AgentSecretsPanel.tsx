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
import { getZkState, type ZkStateOut } from '@/api/vault'
import { EncryptedBadge, VaultEditor, type VaultEntry } from '@/components/vault/VaultEditor'
import { AgentSecretsHomeSection } from '@/pages/agents/AgentSecretsHomeSection'
import { AgentVaultSection } from '@/pages/agents/AgentVaultSection'
import { CredentialReadHistory } from '@/pages/agents/CredentialReadHistory'
import { OpenSecretRequests } from '@/pages/agents/OpenSecretRequests'
import { sections } from '@/pages/agents/agentCredentials'
import { GOG_TOKEN_REF } from '@/pages/agents/googleMint'
import { revealZkValue, setZkValue, unlockVaultKey } from '@/vault/operations'
import { isPasskeyRefusal, PASSKEY_NOT_COMPLETED, PrfUnsupportedError, PRF_UNSUPPORTED } from '@/vault/prf'
import { useUnlockPrompt } from '@/vault/UnlockPrompt'

// The top of the agent's Security tab: where its secrets live, its canopy
// login, and every secret as one table you change in place (`VaultEditor`).
//
// The status route returns booleans and timestamps and never plaintext. The
// agent vault is zero-knowledge (canopy-web#1550): Reveal opens a value HERE
// with the person's own vault key, and canopy-web never holds one it can read.
// Save seals in the browser once the agent has a vault key; before that it
// hands the value to the server, which seals it to the agent's readers and
// keeps no copy.
//
// 1Password is the legacy home: its vault form shows only for an agent still
// set to read from it.

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
}: {
  agent: {
    slug: string
    name?: string
    workspace?: string | null
    email?: string | null
    credential_source?: string | null
  }
  canEdit?: boolean
}) {
  const [rows, setRows] = useState<AgentCredentialStatusOut[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [source, setSource] = useState<string>(agent.credential_source ?? '1password')
  useEffect(() => setSource(agent.credential_source ?? '1password'), [agent.credential_source])
  const [zk, setZk] = useState<ZkStateOut | null>(null)
  const [alerts, setAlerts] = useState<AgentCredentialAlertOut[] | null>(null)
  // A canopy-hosted address has no Google mailbox, so it implies no gog-token.
  const [hosted, setHosted] = useState(false)
  const unlocker = useUnlockPrompt()
  const unlock = () => unlockVaultKey(unlocker.prompt)

  const reload = useCallback(() => {
    getAgentCredentialStatus(agent.slug).then(setRows).catch(() => undefined)
  }, [agent.slug])

  useEffect(() => {
    let cancelled = false
    setRows(null)
    getAgentCredentialStatus(agent.slug)
      .then((r) => !cancelled && setRows(r))
      .catch((e: unknown) => {
        if (cancelled) return
        setError(e instanceof Error ? e.message : 'Failed to load')
        setRows([])
      })
    return () => {
      cancelled = true
    }
  }, [agent.slug])

  useEffect(() => {
    let cancelled = false
    if (!canEdit) return
    getZkState(agent.slug).then((s) => !cancelled && setZk(s), () => undefined)
    getCredentialAlerts(agent.slug).then((a) => !cancelled && setAlerts(a), () => !cancelled && setAlerts([]))
    return () => {
      cancelled = true
    }
  }, [agent.slug, canEdit])

  useEffect(() => {
    if (!agent.email?.trim()) return
    getAgentMailbox(agent.slug).then((m) => setHosted(m.kind === 'hosted'), () => undefined)
  }, [agent.slug, agent.email])

  if (rows === null) return <WorkbenchSkeleton />

  const onCanopy = source === 'canopy-web'
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
        : onCanopy
          ? { label: 'no value', tone: 'warning' }
          : { label: 'from 1Password', tone: 'neutral' },
  }))
  const missing = onCanopy ? sec.fromVault.length : 0
  const byName = new Map(rows.map((r) => [r.name, r]))

  const save = async (name: string, value: string) => {
    if (zk?.enabled) {
      await setZkValue(agent.slug, name, value, unlock)
      setRows(await getAgentCredentialStatus(agent.slug))
    } else {
      setRows(await setAgentCredentials(agent.slug, { [name]: value }))
    }
  }
  const reveal = (name: string) => revealZkValue(agent.slug, name, unlock)
  const remove = async (name: string) => setRows(await deleteAgentCredential(agent.slug, name))

  return (
    <div data-testid="agent-secrets">
      <AgentSecretsHomeSection
        slug={agent.slug}
        credentialSource={source}
        storedNames={rows.filter((r) => r.set).map((r) => r.name)}
        canEdit={canEdit}
        onChanged={reload}
        onSource={setSource}
      />
      {!onCanopy && <AgentVaultSection slug={agent.slug} workspace={agent.workspace} />}

      {canEdit && zk?.enabled && <OpenSecretRequests slug={agent.slug} onStored={reload} />}

      <section aria-labelledby="secrets-title" className="mt-6">
        <h3 id="secrets-title" className="mb-1 text-[13px] font-semibold text-foreground">Its secrets</h3>
        {/* Whether this tab wants anything from the reader. */}
        {(sec.orphans.length > 0 || missing > 0) && (
          <p className="mb-2 text-[12px] text-warning" data-testid="agent-credentials-summary">
            {[
              sec.orphans.length > 0 && `${sec.orphans.length} stored secret(s) nothing declares any more — worth removing.`,
              missing > 0 && `${missing} declared secret(s) have no value yet.`,
            ].filter(Boolean).join(' ')}
          </p>
        )}
        <VaultEditor
          entries={entries}
          canEdit={canEdit}
          onSave={save}
          onReveal={reveal}
          onDelete={remove}
          describeError={reason}
          badge={zk?.enabled ? <EncryptedBadge /> : undefined}
          usage={(e) => {
            const row = byName.get(e.name)
            return row?.set && canEdit ? <CredentialReadHistory slug={agent.slug} row={row} alerts={alerts} /> : null
          }}
        />
      </section>

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
      {unlocker.element}
    </div>
  )
}
