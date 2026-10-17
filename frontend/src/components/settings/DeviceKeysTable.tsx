import { useState } from 'react'
import { approveRunnerDeviceKey, revokeRunnerDeviceKey, type RunnerDeviceKeyOut } from '@/api/deviceKeys'
import { relativeAge } from '@/lib/relativeAge'
import { approveAndGrant, giveAccess, unlockVaultKey } from '@/vault/operations'
import { useUnlockPrompt } from '@/vault/UnlockPrompt'

// Runner device keys (Canopy Vault, canopy-web#1538): the public half of a key a
// box holds, which it signs a DPoP proof with when it reads an agent's secrets.
// A key is trusted only once a person who owns or administers the box approves
// it here. Shared by the account Settings page and an agent's Credentials.

const STATUS_TONE: Record<string, string> = {
  pending: 'bg-warning/10 text-warning border-warning/30',
  active: 'bg-success/10 text-success border-success/30',
  revoked: 'bg-muted text-muted-foreground border-border',
}

export function DeviceKeysTable({
  keys,
  onChange,
  highlight,
}: {
  keys: RunnerDeviceKeyOut[]
  onChange: (next: RunnerDeviceKeyOut) => void
  /** A key id to mark (the `approve_url` a box printed). */
  highlight?: string | null
}) {
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  // Zero-knowledge agents this key now waits on (canopy-web#1550): offered
  // right after approving it, so the box can read them on its next turn.
  const [grant, setGrant] = useState<{ key: string; agents: string[]; done: string[] } | null>(null)
  const unlocker = useUnlockPrompt()

  const grantAll = async (g0 = grant) => {
    if (!g0) return
    setGrant(g0)
    setBusy('grant')
    setError(null)
    try {
      const unlock = () => unlockVaultKey(unlocker.prompt)
      let me: Awaited<ReturnType<typeof unlock>> | null = null
      const once = async () => (me = me ?? (await unlock()))
      for (const slug of g0.agents.filter((a) => !g0.done.includes(a))) {
        await giveAccess(slug, once)
        setGrant((g) => (g ? { ...g, done: [...g.done, slug] } : g))
      }
      const row = keys.find((k) => k.id === g0.key)
      if (row) onChange({ ...row, zk_pending_agents: [] })
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not give this machine access')
    } finally {
      setBusy(null)
    }
  }

  const act = async (id: string, fn: (id: string) => Promise<RunnerDeviceKeyOut>) => {
    setBusy(id)
    setError(null)
    try {
      const next = await fn(id)
      onChange(next)
      if (next.zk_pending_agents?.length) setGrant({ key: id, agents: [...next.zk_pending_agents], done: [] })
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed')
    } finally {
      setBusy(null)
    }
  }

  // Approve = wrap (canopy-web#1550): a key that zero-knowledge agents will
  // wait on is approved and handed their vault keys in ONE passkey touch.
  const approve = async (k: RunnerDeviceKeyOut) => {
    const agents = k.zk_pending_agents ?? []
    if (!agents.length) return act(k.id, approveRunnerDeviceKey)
    setBusy(k.id)
    setError(null)
    try {
      const out = await approveAndGrant(k.id, [...agents], unlocker.prompt)
      onChange(out.key)
      if (out.failed.length) setGrant({ key: k.id, agents: [...out.granted, ...out.failed], done: out.granted })
      else if (out.granted.length) setGrant({ key: k.id, agents: out.granted, done: out.granted })
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed')
    } finally {
      setBusy(null)
    }
  }

  if (keys.length === 0) {
    return (
      <p className="text-[12px] text-muted-foreground" data-testid="device-keys-empty">
        No device keys registered.
      </p>
    )
  }

  return (
    <>
      <table className="w-full text-[12px]" data-testid="device-keys">
        <thead>
          <tr className="text-left text-[11px] uppercase tracking-wide text-muted-foreground">
            <th className="py-1 font-medium">Runner</th>
            <th className="py-1 font-medium">Machine</th>
            <th className="py-1 font-medium">Status</th>
            <th className="py-1 font-medium">Last used</th>
            <th className="py-1" />
          </tr>
        </thead>
        <tbody className="divide-y divide-border">
          {keys.map((k) => (
            <tr
              key={k.id}
              data-testid={`device-key-${k.id}`}
              className={highlight === k.id ? 'bg-primary/5' : undefined}
            >
              <td className="py-1.5 text-foreground">{k.runner_name}</td>
              <td className="py-1.5">
                {/* The key's id (an EC2 instance role, or a key thumbprint) is on
                    hover: it tells one key from another, but nobody reads it. */}
                <span className="text-foreground" title={k.kind === 'aws-iam' && k.role_arn ? k.role_arn : k.jkt}>
                  {k.label || k.kind}
                </span>
              </td>
              <td className="py-1.5">
                <span className={`rounded border px-1.5 py-0.5 text-[11px] ${STATUS_TONE[k.status] ?? ''}`}>
                  {k.status}
                </span>
                {k.status === 'pending' && k.reapproval && (
                  // Not a new box: it added (or changed) the key zero-knowledge
                  // agents' vault keys are wrapped to (canopy-web#1550).
                  <span className="ml-2 text-[11px] text-muted-foreground" data-testid={`device-key-reapproval-${k.id}`}>
                    re-approval: encryption key added
                  </span>
                )}
                {k.status === 'active' && k.approved_by_email && (
                  <span className="ml-2 text-[11px] text-muted-foreground">by {k.approved_by_email}</span>
                )}
              </td>
              <td className="py-1.5 text-muted-foreground">{k.last_used_at ? relativeAge(k.last_used_at) : '—'}</td>
              <td className="py-1.5 text-right">
                {/* An approved machine that agents still wait on (approved
                    before they existed, or by someone else): one touch lets it in. */}
                {k.can_manage && k.status === 'active' && (k.zk_pending_agents ?? []).length > 0 && (
                  <button
                    type="button"
                    data-testid={`device-key-give-${k.id}`}
                    onClick={() => void grantAll({ key: k.id, agents: [...(k.zk_pending_agents ?? [])], done: [] })}
                    disabled={busy !== null}
                    className="min-h-11 rounded-md bg-primary px-3 py-1 text-[12px] font-medium text-primary-foreground disabled:opacity-40 sm:min-h-0"
                  >
                    Give access
                  </button>
                )}
                {k.can_manage && k.status === 'pending' && (
                  <button
                    type="button"
                    onClick={() => void approve(k)}
                    disabled={busy !== null}
                    className="min-h-11 rounded-md bg-primary px-3 py-1 text-[12px] font-medium text-primary-foreground disabled:opacity-40 sm:min-h-0"
                  >
                    Approve
                  </button>
                )}
                {k.can_manage && k.status !== 'revoked' && (
                  <button
                    type="button"
                    onClick={() => {
                      if (window.confirm(`Revoke the key "${k.label || k.kind}" on ${k.runner_name}? It stops working immediately.`)) {
                        void act(k.id, revokeRunnerDeviceKey)
                      }
                    }}
                    disabled={busy !== null}
                    className="ml-2 min-h-11 rounded-md border border-input px-3 py-1 text-[12px] text-foreground hover:border-destructive hover:text-destructive disabled:opacity-40 sm:min-h-0"
                  >
                    Revoke
                  </button>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {grant && (
        <div className="mt-2 flex flex-wrap items-center gap-2 rounded-md border border-warning/30 bg-warning/10 px-3 py-2 text-[12px]"
             data-testid="device-key-zk-grant">
          <span className="text-foreground">
            {grant.done.length === grant.agents.length
              ? `Done: this machine can now read ${grant.agents.join(', ')}.`
              : `This machine is waiting to read ${grant.agents.join(', ')}.`}
          </span>
          {grant.done.length < grant.agents.length && (
            <button type="button" onClick={() => void grantAll()} disabled={busy !== null}
                    data-testid="device-key-zk-grant-button"
                    className="min-h-11 rounded-md bg-primary px-3 py-1 text-[12px] font-medium text-primary-foreground disabled:opacity-40 sm:min-h-0">
              Give it access
            </button>
          )}
          <button type="button" onClick={() => setGrant(null)} className="text-muted-foreground hover:text-foreground">
            Dismiss
          </button>
          {unlocker.element}
        </div>
      )}
      {!grant && unlocker.element}
      {error && (
        <p className="mt-2 text-[12px] text-destructive" data-testid="device-keys-error">
          {error}
        </p>
      )}
    </>
  )
}
