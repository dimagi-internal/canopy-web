// The pieces of the capabilities page: one capability's row, its grant form, its leases and limits. The page and its data are `AgentCapabilitiesSection`.
import { useState, type FormEvent } from 'react'
import { Button, Input } from 'canopy-ui/ui'
import {
  revokeCapabilityGrant,
  setCapabilityGrant,
  type AgentCapabilityOut,
  type CapabilityLeaseOut,
} from '@/api/capabilities'
import { relativeAge } from '@/lib/relativeAge'
import { type Limits, manifestLimits, manifestSlots, limitsText, duration, optionalInt } from './capabilityLimits'

export function CapabilityRow({
  cap,
  agentSlug,
  canGrant,
  onChanged,
}: {
  cap: AgentCapabilityOut
  agentSlug: string
  canGrant: boolean
  onChanged: () => void
}) {
  const [editing, setEditing] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const ceiling = manifestLimits(cap)
  const slots = manifestSlots(cap)
  const grant = cap.grant

  const revoke = async () => {
    if (!grant) return
    const n = cap.live_leases.length
    const also = n ? ` Its ${n} live lease${n === 1 ? '' : 's'} will be released.` : ''
    if (!window.confirm(`Revoke ${cap.name} for ${agentSlug}?${also}`)) return
    setBusy(true)
    setError(null)
    try {
      await revokeCapabilityGrant(grant.id)
      onChanged()
    } catch (e) {
      setError(e instanceof Error ? e.message : 'The revoke was refused')
    } finally {
      setBusy(false)
    }
  }

  return (
    <>
      <tr className="align-top" data-testid={`capability-${cap.name}`}>
        <td className="px-3 py-2">
          <div className="font-medium text-foreground">
            <code>{cap.name}</code>
          </div>
          {cap.description && <div className="mt-0.5 text-muted-foreground">{cap.description}</div>}
        </td>
        <td className="px-3 py-2 text-foreground-secondary">{cap.kind}</td>
        <td className="px-3 py-2 text-foreground-secondary">
          {cap.kind === 'pool' ? (
            <span>
              {slots.length} slot{slots.length === 1 ? '' : 's'}: {slots.join(', ')}
            </span>
          ) : (
            <span>
              {cap.region || '—'}
              {cap.stack && <span className="block text-muted-foreground">{cap.stack}</span>}
            </span>
          )}
        </td>
        <td className="px-3 py-2 text-foreground-secondary">
          <LimitsCell effective={cap.limits} ceiling={ceiling} />
        </td>
        <td className="px-3 py-2">
          {grant ? (
            <span>
              <span className="rounded border border-success/30 bg-success/10 px-1.5 py-0.5 text-success">
                Granted
              </span>
              <span className="mt-1 block text-muted-foreground">
                {grant.created_by ? `by ${grant.created_by}, ` : ''}
                {relativeAge(grant.created_at)}
              </span>
              {(grant.max_concurrent || grant.max_lease_s) && (
                <span className="block text-muted-foreground">
                  lowered to{' '}
                  {[
                    grant.max_concurrent ? `${grant.max_concurrent} at once` : null,
                    grant.max_lease_s ? `${duration(grant.max_lease_s)} a lease` : null,
                  ]
                    .filter(Boolean)
                    .join(', ')}
                </span>
              )}
            </span>
          ) : (
            <span className="rounded border border-border px-1.5 py-0.5 text-muted-foreground">Not granted</span>
          )}
        </td>
        {canGrant && (
          <td className="whitespace-nowrap px-3 py-2 text-right">
            {grant ? (
              <span className="inline-flex gap-1.5">
                <Button size="xs" variant="outline" disabled={busy} onClick={() => setEditing((v) => !v)}>
                  Limits
                </Button>
                <Button size="xs" variant="destructive" disabled={busy} onClick={() => void revoke()}>
                  Revoke
                </Button>
              </span>
            ) : (
              <Button size="xs" disabled={busy} onClick={() => setEditing((v) => !v)}>
                Grant
              </Button>
            )}
          </td>
        )}
      </tr>
      {error && (
        <tr>
          <td colSpan={6} className="px-3 pb-2">
            <p role="alert" className="text-[12px] text-destructive">
              {error}
            </p>
          </td>
        </tr>
      )}
      {canGrant && editing && (
        <tr>
          <td colSpan={6} className="bg-muted/40 px-3 py-3">
            <GrantForm
              cap={cap}
              agentSlug={agentSlug}
              ceiling={ceiling}
              onCancel={() => setEditing(false)}
              onSaved={() => {
                setEditing(false)
                onChanged()
              }}
            />
          </td>
        </tr>
      )}
    </>
  )
}

export function GrantForm({
  cap,
  agentSlug,
  ceiling,
  onCancel,
  onSaved,
}: {
  cap: AgentCapabilityOut
  agentSlug: string
  ceiling: Limits
  onCancel: () => void
  onSaved: () => void
}) {
  const [concurrent, setConcurrent] = useState(cap.grant?.max_concurrent ? String(cap.grant.max_concurrent) : '')
  const [leaseS, setLeaseS] = useState(cap.grant?.max_lease_s ? String(cap.grant.max_lease_s) : '')
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const submit = async (e: FormEvent) => {
    e.preventDefault()
    setSaving(true)
    setError(null)
    try {
      await setCapabilityGrant(agentSlug, cap.name, {
        max_concurrent: optionalInt(concurrent),
        max_lease_s: optionalInt(leaseS),
      })
      onSaved()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'The grant was refused')
    } finally {
      setSaving(false)
    }
  }

  return (
    <form onSubmit={(e) => void submit(e)} className="flex flex-wrap items-end gap-3" aria-label={`Grant ${cap.name}`}>
      <label className="flex flex-col gap-1 text-[12px] text-muted-foreground">
        Max at once (optional)
        <Input
          type="number"
          inputMode="numeric"
          min={1}
          className="w-32"
          value={concurrent}
          onChange={(e) => setConcurrent(e.target.value)}
          placeholder={ceiling.max_concurrent ? String(ceiling.max_concurrent) : ''}
        />
      </label>
      <label className="flex flex-col gap-1 text-[12px] text-muted-foreground">
        Max lease, seconds (optional)
        <Input
          type="number"
          inputMode="numeric"
          min={60}
          className="w-40"
          value={leaseS}
          onChange={(e) => setLeaseS(e.target.value)}
          placeholder={ceiling.max_lease_s ? String(ceiling.max_lease_s) : ''}
        />
      </label>
      <div className="flex gap-1.5">
        <Button type="submit" size="sm" disabled={saving}>
          {cap.grant ? 'Save limits' : 'Grant'}
        </Button>
        <Button type="button" size="sm" variant="ghost" onClick={onCancel} disabled={saving}>
          Cancel
        </Button>
      </div>
      <p className="basis-full text-[11px] text-muted-foreground">
        Leave blank to use the manifest's limits
        {ceiling.max_concurrent || ceiling.max_lease_s
          ? ` (${limitsText(ceiling)})`
          : ''}
        . A grant can only lower them, never raise them.
      </p>
      {error && (
        <p role="alert" className="basis-full text-[12px] text-destructive">
          {error}
        </p>
      )}
    </form>
  )
}

export function LeaseTable({ leases }: { leases: CapabilityLeaseOut[] }) {
  return (
    <div className="overflow-x-auto rounded-lg border border-border bg-card">
      <table className="w-full text-left text-[12px]">
        <thead className="border-b border-border text-muted-foreground">
          <tr>
            <th className="px-3 py-2 font-medium">Capability</th>
            <th className="px-3 py-2 font-medium">Status</th>
            <th className="px-3 py-2 font-medium">Slot / machine</th>
            <th className="px-3 py-2 font-medium">Holder</th>
            <th className="px-3 py-2 font-medium">Since</th>
            <th className="px-3 py-2 font-medium">Expires</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-border">
          {leases.map((l) => (
            <tr key={l.lease_id} data-testid={`lease-${l.lease_id}`}>
              <td className="px-3 py-2">
                <code>{l.capability}</code>
              </td>
              <td className="px-3 py-2 text-foreground-secondary">
                {l.status}
                {l.status === 'queued' && l.position ? ` (#${l.position})` : ''}
              </td>
              <td className="px-3 py-2 text-foreground-secondary">{l.slot || l.instance_id || '—'}</td>
              <td className="px-3 py-2 text-foreground-secondary">
                {l.turn_id ? `turn ${l.turn_id.slice(0, 8)}` : `${l.agent} (no turn)`}
              </td>
              <td className="px-3 py-2 text-muted-foreground">{relativeAge(l.created_at)}</td>
              <td className="px-3 py-2 text-muted-foreground">
                {l.expires_at ? new Date(l.expires_at).toLocaleString() : '—'}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

export function LimitsCell({ effective, ceiling }: { effective: Record<string, number>; ceiling: Limits }) {
  const lowered =
    (ceiling.max_concurrent !== undefined && effective.max_concurrent < ceiling.max_concurrent) ||
    (ceiling.max_lease_s !== undefined && effective.max_lease_s < ceiling.max_lease_s)
  return (
    <span>
      {limitsText(effective)}
      {lowered && <span className="block text-muted-foreground">manifest: {limitsText(ceiling)}</span>}
    </span>
  )
}
