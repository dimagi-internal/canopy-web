import { type JSX } from 'react'
import { relativeAge } from '@/lib/relativeAge'
import { type ConnectedApp } from '@/api/connectedApps'
import { STATUS_STYLE } from '@/pages/connectedApps/ConnectionTester'

export function ago(iso: string | null | undefined, now: Date): string {
  return iso ? relativeAge(iso, now, { seconds: false }) : '—'
}

/** The live probe's last verdict as a badge: pass, fail, or not set up. */
function ProbeBadge({ probe }: { probe: ConnectedApp['live_probe'] }): JSX.Element {
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
  apps: readonly ConnectedApp[]
  now?: Date
}): JSX.Element | null {
  const rows = apps.filter((a) => a.issues_host_grants && !a.revoked)
  if (rows.length === 0) return null
  return (
    <section className="space-y-2">
      <h2 className="text-sm font-medium text-foreground">Acting as visitors</h2>
      <table className="w-full border-collapse text-xs">
        <thead>
          <tr className="border-b border-border text-left text-muted-foreground">
            <th className="py-1 pr-2 font-normal">Site</th>
            <th className="py-1 pr-2 font-normal">Live probe</th>
            <th className="py-1 pr-2 font-normal">Probed</th>
            <th className="py-1 pr-2 font-normal">Last grant</th>
            <th className="py-1 pr-2 font-normal">Last call</th>
            <th className="py-1 pr-2 text-right font-normal">Refusals 24h</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((app) => (
            <tr key={app.id} className="border-b border-border align-top last:border-0">
              <td className="py-1 pr-2 text-foreground">{app.name}</td>
              <td className="py-1 pr-2">
                <ProbeBadge probe={app.live_probe} />
                {app.live_probe.ok !== true && app.live_probe.step_label && (
                  <span
                    className="ml-2 text-foreground-secondary"
                    title={app.live_probe.reason}
                  >
                    {app.live_probe.ok === false ? app.live_probe.step_label : app.live_probe.reason}
                  </span>
                )}
              </td>
              <td className="py-1 pr-2 text-muted-foreground">{ago(app.live_probe.at, now)}</td>
              <td className="py-1 pr-2 text-muted-foreground">{ago(app.traffic.last_redeemed_at, now)}</td>
              <td className="py-1 pr-2 text-muted-foreground">{ago(app.traffic.last_site_call_at, now)}</td>
              <td
                className={`py-1 pr-2 text-right tabular-nums ${
                  app.traffic.refusals_24h > 0 ? 'text-warning' : 'text-muted-foreground'
                }`}
              >
                {app.traffic.refusals_24h}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  )
}
