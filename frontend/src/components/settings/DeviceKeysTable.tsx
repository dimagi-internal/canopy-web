import { useEffect, useState } from 'react'
import { approveRunnerDeviceKey, revokeRunnerDeviceKey, type RunnerDeviceKeyOut } from '@/api/deviceKeys'
import { relativeAge } from '@/lib/relativeAge'
import { approveAndGrant, giveAccess, unlockVaultKey, vouchForMyMachine } from '@/vault/operations'
import { useUnlockPrompt } from '@/vault/UnlockPrompt'
import { canOpenText, keyPlace, machineDescription } from './machineText'

// The machines that may open agents' secrets (Canopy Vault, canopy-web#1538,
// #1550). Each holds a key made inside its own hardware; approving one (a
// passkey touch) hands it a copy of the key to the agents it runs. Shown as one
// plain line per machine — which machine, where its key lives, what it can
// open. Shared by the account Settings page and an agent's Credentials.

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
  // Revoked keys are history: out of the way unless asked for (or linked to).
  const [showRevoked, setShowRevoked] = useState(false)
  const revoked = keys.filter((k) => k.status === 'revoked' && k.id !== highlight)
  const shown = showRevoked ? keys : keys.filter((k) => !revoked.includes(k))
  // A link to one machine (a setup step's "Let … in", a box's approve_url) lands on
  // its row, not the top of a long Settings page.
  const marked = !!highlight && keys.some((k) => k.id === highlight)
  useEffect(() => {
    if (marked) document.querySelector(`[data-testid="device-key-${highlight}"]`)?.scrollIntoView?.({ block: 'center' })
  }, [marked, highlight])

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
      setError(e instanceof Error ? e.message : 'Could not give this runner access')
    } finally {
      setBusy(null)
    }
  }

  // My own machine, for agents whose vault I am a member of but do not manage: one
  // passkey touch signs for it, and a keeper hands it the key within minutes.
  const vouch = async (k: RunnerDeviceKeyOut) => {
    setBusy(k.id)
    setError(null)
    try {
      const me = await unlockVaultKey(unlocker.prompt)
      const done = await vouchForMyMachine(k, k.zk_vouch_agents ?? [], me)
      onChange({ ...k, zk_vouch_agents: (k.zk_vouch_agents ?? []).filter((a) => !done.includes(a.slug)) })
      if (done.length < (k.zk_vouch_agents ?? []).length) setError('Could not confirm this machine for every agent')
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not confirm this machine')
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
    const mine = k.zk_vouch_agents ?? []
    if (!agents.length && !mine.length) return act(k.id, approveRunnerDeviceKey)
    setBusy(k.id)
    setError(null)
    try {
      const out = await approveAndGrant(k.id, [...agents], unlocker.prompt, [...mine])
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
        No runners yet.
      </p>
    )
  }

  return (
    <>
      <ul className="divide-y divide-border" data-testid="device-keys">
        {shown.map((k) => {
          const opens = canOpenText(k)
          return (
            <li
              key={k.id}
              data-testid={`device-key-${k.id}`}
              className={`flex flex-col gap-2 py-2 sm:flex-row sm:items-center sm:justify-between ${highlight === k.id ? 'rounded-md bg-primary/5 px-2' : ''}`}
            >
              <div className="min-w-0 space-y-0.5">
                {/* One line per machine: which one it is, where its key lives,
                    what it can open. The key's id (an EC2 instance role, or a
                    thumbprint) tells one key from another on hover; nobody reads it. */}
                <p className="text-[13px] text-foreground" title={k.kind === 'aws-iam' && k.role_arn ? k.role_arn : k.jkt}
                   data-testid={`device-key-line-${k.id}`}>
                  <span className="font-medium">{k.runner_name}</span>
                  <span className="text-muted-foreground">
                    {' — '}{machineDescription(k)} · {keyPlace(k)}{opens ? ` · ${opens}` : ''}
                  </span>
                </p>
                <p className="text-[12px]" data-testid={`device-key-status-${k.id}`}>
                  {k.status === 'pending' ? (
                    <span className="text-warning">
                      Waiting for your approval — approving gives it a copy of the key to your agents&rsquo; secrets
                    </span>
                  ) : k.status === 'revoked' ? (
                    <span className="text-muted-foreground">Revoked — it can no longer open anything</span>
                  ) : k.ecdh_waiting ? (
                    // Still online and running other agents; it just cannot open
                    // private secrets until its new key is approved.
                    <span className="text-warning" data-testid={`device-key-ecdh-waiting-${k.id}`}>
                      New key waiting — approve to give it access
                    </span>
                  ) : (
                    <span className="text-muted-foreground">
                      Approved{k.approved_by_email ? ` by ${k.approved_by_email}` : ''}
                      {k.last_used_at ? ` · last used ${relativeAge(k.last_used_at)}` : ''}
                    </span>
                  )}
                </p>
              </div>
              <div className="flex shrink-0 flex-wrap gap-2">
                {/* An approved machine that agents still wait on (approved
                    before they existed, or by someone else): one touch lets it in. */}
                {k.can_manage && k.status === 'active' && !k.ecdh_waiting && (k.zk_pending_agents ?? []).length > 0 && (
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
                {/* My own approved machine that an agent whose vault I am only a
                    member of still waits on: I sign for it, a keeper grants it. */}
                {k.can_manage && k.status === 'active' && !k.ecdh_waiting && (k.zk_vouch_agents ?? []).length > 0 && (
                  <button
                    type="button"
                    data-testid={`device-key-vouch-${k.id}`}
                    onClick={() => void vouch(k)}
                    disabled={busy !== null}
                    className="min-h-11 rounded-md bg-primary px-3 py-1 text-[12px] font-medium text-primary-foreground disabled:opacity-40 sm:min-h-0"
                  >
                    Confirm access
                  </button>
                )}
                {k.can_manage && (k.status === 'pending' || k.ecdh_waiting) && (
                  <button
                    type="button"
                    data-testid={`device-key-approve-${k.id}`}
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
                      if (window.confirm(`Revoke ${k.runner_name}? It stops being able to open your agents’ secrets immediately.`)) {
                        void act(k.id, revokeRunnerDeviceKey)
                      }
                    }}
                    disabled={busy !== null}
                    className="min-h-11 rounded-md border border-input px-3 py-1 text-[12px] text-foreground hover:border-destructive hover:text-destructive disabled:opacity-40 sm:min-h-0"
                  >
                    Revoke
                  </button>
                )}
              </div>
            </li>
          )
        })}
      </ul>
      {revoked.length > 0 && (
        <button type="button" className="mt-1 text-[12px] text-primary hover:underline" data-testid="device-keys-show-revoked"
                onClick={() => setShowRevoked((v) => !v)}>
          {showRevoked ? 'Hide revoked' : `Show revoked (${revoked.length})`}
        </button>
      )}
      {grant && (
        <div className="mt-2 flex flex-wrap items-center gap-2 rounded-md border border-warning/30 bg-warning/10 px-3 py-2 text-[12px]"
             data-testid="device-key-zk-grant">
          <span className="text-foreground">
            {grant.done.length === grant.agents.length
              ? `Done: this runner can now open ${grant.agents.join(', ')}.`
              : `This runner is waiting to open ${grant.agents.join(', ')}.`}
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
