import { useEffect, useState } from 'react'
import { getAgentDeviceKeys, setDeviceBinding, type RunnerDeviceKeyOut } from '@/api/deviceKeys'
import { DeviceKeysTable } from '@/components/settings/DeviceKeysTable'

// Device binding for this agent (Canopy Vault, canopy-web#1538). With it on and
// the agent resolving from canopy-web, a box must prove it holds an APPROVED
// device key to read the agent's secrets; a token alone no longer does. The keys
// listed are those on runners this agent routes to or its owner owns.

export function AgentDeviceKeysSection({
  slug,
  credentialSource,
  requireDeviceBinding,
  canEdit,
  onLoaded,
}: {
  slug: string
  credentialSource?: string | null
  requireDeviceBinding?: boolean | null
  canEdit: boolean
  /** Told the machines' keys once read, for the vault's summary. */
  onLoaded?: (keys: RunnerDeviceKeyOut[]) => void
}) {
  const [keys, setKeys] = useState<RunnerDeviceKeyOut[] | null>(null)
  const [required, setRequired] = useState(Boolean(requireDeviceBinding))
  const [error, setError] = useState<string | null>(null)

  useEffect(() => setRequired(Boolean(requireDeviceBinding)), [requireDeviceBinding])

  useEffect(() => {
    let cancelled = false
    if (!canEdit) return
    getAgentDeviceKeys(slug)
      .then((k) => { if (!cancelled) { setKeys(k); onLoaded?.(k) } })
      .catch(() => { if (!cancelled) { setKeys([]); onLoaded?.([]) } })
    return () => {
      cancelled = true
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [slug, canEdit])

  if (!canEdit) return null

  const toggle = async () => {
    setError(null)
    try {
      setRequired(await setDeviceBinding(slug, !required))
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to save')
    }
  }

  const active = (keys ?? []).filter((k) => k.status === 'active').length
  return (
    <section id="cred-device-keys" className="mb-6" data-testid="agent-device-keys">
      <h4 className="mb-2 text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
        Machines
      </h4>
      <label className="mb-2 flex items-center gap-2 text-[12px] text-foreground">
        <input
          type="checkbox"
          checked={required}
          onChange={() => void toggle()}
          aria-label="Only approved machines may read secrets"
          data-testid="require-device-binding"
        />
        Only machines you approved may read this agent&rsquo;s secrets
      </label>
      {required && credentialSource !== 'canopy-web' && (
        <p className="mb-2 text-[11px] text-warning">
          Applies only when secrets resolve from canopy-web; this agent resolves from 1Password.
        </p>
      )}
      {required && credentialSource === 'canopy-web' && keys !== null && active === 0 && (
        <p className="mb-2 text-[11px] text-destructive" data-testid="no-active-device-key">
          No approved machine yet: none can read this agent&rsquo;s secrets until you approve one.
        </p>
      )}
      {keys === null ? (
        <p className="text-[11px] text-muted-foreground">…</p>
      ) : (
        <DeviceKeysTable
          keys={keys}
          onChange={(next) => setKeys((ks) => (ks ?? []).map((k) => (k.id === next.id ? next : k)))}
        />
      )}
      {error && <p className="mt-2 text-[12px] text-destructive">{error}</p>}
    </section>
  )
}
