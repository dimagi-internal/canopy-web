import { useState } from 'react'
import { approveRunnerDeviceKey, revokeRunnerDeviceKey, type RunnerDeviceKeyOut } from '@/api/deviceKeys'
import { relativeAge } from '@/lib/relativeAge'

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

  const act = async (id: string, fn: (id: string) => Promise<RunnerDeviceKeyOut>) => {
    setBusy(id)
    setError(null)
    try {
      onChange(await fn(id))
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
            <th className="py-1 font-medium">Key</th>
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
                <span className="text-foreground">{k.label || k.kind}</span>
                <span className="ml-2 font-mono text-[11px] text-muted-foreground" title={k.jkt}>
                  {k.jkt.slice(0, 12)}…
                </span>
              </td>
              <td className="py-1.5">
                <span className={`rounded border px-1.5 py-0.5 text-[11px] ${STATUS_TONE[k.status] ?? ''}`}>
                  {k.status}
                </span>
                {k.status === 'active' && k.approved_by_email && (
                  <span className="ml-2 text-[11px] text-muted-foreground">by {k.approved_by_email}</span>
                )}
              </td>
              <td className="py-1.5 text-muted-foreground">{k.last_used_at ? relativeAge(k.last_used_at) : '—'}</td>
              <td className="py-1.5 text-right">
                {k.can_manage && k.status === 'pending' && (
                  <button
                    type="button"
                    onClick={() => void act(k.id, approveRunnerDeviceKey)}
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
      {error && (
        <p className="mt-2 text-[12px] text-destructive" data-testid="device-keys-error">
          {error}
        </p>
      )}
    </>
  )
}
