// Reading a capability's limits and slots from its manifest, and saying them in words.
import {
  type AgentCapabilityOut,
  } from '@/api/capabilities'

export interface Limits {
  max_concurrent?: number
  max_lease_s?: number
  idle_release_s?: number
}

/** The manifest's own limits — the ceiling a grant may lower. */
export function manifestLimits(cap: AgentCapabilityOut): Limits {
  const raw = (cap.manifest as { limits?: Record<string, unknown> }).limits ?? {}
  const num = (v: unknown) => (typeof v === 'number' ? v : undefined)
  return {
    max_concurrent: num(raw.max_concurrent),
    max_lease_s: num(raw.max_lease_s),
    idle_release_s: num(raw.idle_release_s),
  }
}

export function manifestSlots(cap: AgentCapabilityOut): string[] {
  const slots = (cap.manifest as { slots?: unknown }).slots
  return Array.isArray(slots) ? slots.filter((s): s is string => typeof s === 'string') : []
}

export function limitsText(l: Limits): string {
  const parts: string[] = []
  if (l.max_concurrent !== undefined) parts.push(`${l.max_concurrent} at once`)
  if (l.max_lease_s !== undefined) parts.push(`${duration(l.max_lease_s)} a lease`)
  if (l.idle_release_s !== undefined) parts.push(`released after ${duration(l.idle_release_s)} idle`)
  return parts.join(' · ')
}

/** Seconds as the unit a person reads at a glance: 90s, 15m, 1h, 1h 30m, 2d. */
export function duration(s: number): string {
  if (s < 120) return `${s}s`
  if (s < 3600) return `${Math.round(s / 60)}m`
  if (s < 172800 || s % 86400 !== 0) {
    const h = Math.floor(s / 3600)
    const m = Math.round((s % 3600) / 60)
    return m ? `${h}h ${m}m` : `${h}h`
  }
  return `${s / 86400}d`
}

export function optionalInt(v: string): number | null {
  const t = v.trim()
  if (!t) return null
  const n = Number(t)
  return Number.isFinite(n) ? Math.trunc(n) : null
}
