import { type JSX } from 'react'
import { relativeAge } from '@/lib/relativeAge'
import { type HostSite } from '@/api/hostSites'
import { STATUS_STYLE } from '@/pages/hostSites/ConnectionTester'
import { Td, Th, Tr } from '@/components/DataTable'

export function ago(iso: string | null | undefined, now: Date): string {
  return iso ? relativeAge(iso, now, { seconds: false }) : '—'
}

/** The live probe's last verdict as a badge: pass, fail, or not set up. */
function ProbeBadge({ probe }: { probe: HostSite['live_probe'] }): JSX.Element {
  const [label, style] =
    probe.ok === true
      ? ['pass', STATUS_STYLE.pass]
      : probe.ok === false
        ? ['fail', STATUS_STYLE.fail]
        : [probe.at ? 'not set up' : 'not run', STATUS_STYLE.skip]
  return (
    <span className={`inline-block rounded border px-1.5 text-[10px] uppercase ${style}`}>{label}</span>
  )
}

/**
 * Every site that lets an agent act as its visitors, and whether that works:
 * canopy's live probe (a real grant for the site's probe user, every 30 minutes)
 * beside what real visitors' traffic says. A site that stops working shows here
 * before a visitor finds out.
 */
export function SiteHealthTable({
  apps,
  now = new Date(),
}: {
  apps: readonly HostSite[]
  now?: Date
}): JSX.Element | null {
  const rows = apps.filter((a) => a.issues_host_grants && !a.revoked)
  if (rows.length === 0) return null
  return (
    <section className="space-y-2">
      <h2 className="text-sm font-medium text-foreground">Acting as visitors</h2>
      <table className="w-full border-collapse text-xs">
        <thead>
          <Tr rule="bottom" head="plain">
            <Th density="compact-gutter" weight="normal">Site</Th>
            <Th density="compact-gutter" weight="normal">Live probe</Th>
            <Th density="compact-gutter" weight="normal">Probed</Th>
            <Th density="compact-gutter" weight="normal">Last grant</Th>
            <Th density="compact-gutter" weight="normal">Last call</Th>
            <Th density="compact-gutter" weight="normal" align="right">Refusals 24h</Th>
          </Tr>
        </thead>
        <tbody>
          {rows.map((app) => (
            <Tr rule="bottom-last" top key={app.id}>
              <Td density="compact-gutter" tone="foreground">{app.name}</Td>
              <Td density="compact-gutter">
                <ProbeBadge probe={app.live_probe} />
                {app.live_probe.ok !== true && app.live_probe.step_label && (
                  <span
                    className="ml-2 text-foreground-secondary"
                    title={app.live_probe.reason}
                  >
                    {app.live_probe.ok === false ? app.live_probe.step_label : app.live_probe.reason}
                  </span>
                )}
              </Td>
              <Td density="compact-gutter" tone="muted">{ago(app.live_probe.at, now)}</Td>
              <Td density="compact-gutter" tone="muted">{ago(app.traffic.last_redeemed_at, now)}</Td>
              <Td density="compact-gutter" tone="muted">{ago(app.traffic.last_site_call_at, now)}</Td>
              <Td
                density="compact-gutter"
                align="right"
                numeric
                className={app.traffic.refusals_24h > 0 ? 'text-warning' : 'text-muted-foreground'}
              >
                {app.traffic.refusals_24h}
              </Td>
            </Tr>
          ))}
        </tbody>
      </table>
    </section>
  )
}
