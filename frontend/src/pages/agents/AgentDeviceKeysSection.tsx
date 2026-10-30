import { useEffect, useState } from 'react'
import { getAgentDeviceKeys, type RunnerDeviceKeyOut } from '@/api/deviceKeys'
import { LoadFailed } from '@/components/vault/LoadFailed'
import { DeviceKeysTable } from '@/components/settings/DeviceKeysTable'

// The machines that can read this agent's secrets (Canopy Vault, canopy-web#1538).
// Each holds a key made inside its own hardware; approving one (a passkey touch)
// gives it a copy of the vault's key, and a read needs the machine to prove it holds
// that key — a login token alone opens nothing. The keys listed are those on runners
// this agent routes to or its owner owns.

export function AgentDeviceKeysSection({
  slug,
  canEdit,
  onLoaded,
}: {
  slug: string
  canEdit: boolean
  /** Told the machines' keys once read, for the vault's summary. */
  onLoaded?: (keys: RunnerDeviceKeyOut[]) => void
}) {
  const [keys, setKeys] = useState<RunnerDeviceKeyOut[] | null>(null)
  const [failed, setFailed] = useState(false)

  useEffect(() => {
    let cancelled = false
    if (!canEdit) return
    getAgentDeviceKeys(slug)
      .then((k) => { if (!cancelled) { setKeys(k); onLoaded?.(k) } })
      .catch(() => { if (!cancelled) setFailed(true) })
    return () => {
      cancelled = true
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [slug, canEdit])

  if (!canEdit) return null

  const active = (keys ?? []).filter((k) => k.status === 'active').length
  return (
    <section id="cred-device-keys" className="mb-6" data-testid="agent-device-keys">
      <h4 className="mb-2 text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
        Machines that can read these secrets
      </h4>
      <p className="mb-2 text-[12px] text-muted-foreground">
        A machine you approve can read this agent&rsquo;s secrets; one you don&rsquo;t, can&rsquo;t.
      </p>
      {failed && <LoadFailed what="the machines" />}
      {keys !== null && active === 0 && (
        <p className="mb-2 text-[11px] text-destructive" data-testid="no-active-device-key">
          No approved machine yet: none can read this agent&rsquo;s secrets until you approve one.
        </p>
      )}
      {failed ? null : keys === null ? (
        <p className="text-[11px] text-muted-foreground">…</p>
      ) : (
        <DeviceKeysTable
          keys={keys}
          onChange={(next) => {
            const updated = (keys ?? []).map((k) => (k.id === next.id ? next : k))
            setKeys(updated)
            onLoaded?.(updated)
          }}
        />
      )}
    </section>
  )
}
