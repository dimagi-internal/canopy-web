import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { listRunners, type RunnerOut } from '@/api/harness'
import { getPresencePreference, setPresencePreference } from '@/api/presence'
import { notifyPresencePreferenceChanged } from '@/presence/events'
import { TokensPanel } from '@/components/settings/TokensPanel'
import { GitHubPanel } from '@/components/settings/GitHubPanel'

export function SettingsPage() {
  // Presence visibility. Defaults to visible (matches the backend default
  // when no preference row exists yet) so the checkbox never flashes
  // unchecked before the fetch resolves.
  const [showPresence, setShowPresence] = useState(true)

  useEffect(() => {
    getPresencePreference()
      .then((pref) => setShowPresence(pref.show_presence))
      .catch(() => {
        // Presence is an enhancement — a failed fetch just leaves the
        // default (visible) in place rather than surfacing an error here.
      })
  }, [])

  async function togglePresence(next: boolean) {
    setShowPresence(next) // optimistic
    try {
      await setPresencePreference(next)
      // The presence socket only re-reads visibility on its next
      // `presence.enter` (i.e. the next navigation) — tell every open tab's
      // socket to reconnect right now so opting out takes effect
      // immediately instead of leaving the user visible until each tab
      // happens to navigate. See AppLayout's PresenceHeaderBadge +
      // usePresenceReconnectNonce.
      notifyPresencePreferenceChanged()
    } catch {
      // Revert the optimistic update — the change didn't actually persist.
      setShowPresence(!next)
    }
  }

  // There is no "AI backend" here any more. canopy-web makes no model calls of
  // its own — every turn runs on a runner, under THAT box's Claude login — so
  // the old "Connect Claude Subscription" panel configured something nothing
  // used. What a person does own is the runners they paired: RunnersPanel
  // points at where their Claude logins live.
  return (
    <div className="space-y-5 max-w-xl">
      <div>
        <h1 className="text-lg font-semibold text-foreground">Settings</h1>
        <p className="mt-0.5 text-xs text-muted-foreground">
          Your account: the runners you administer, presence, GitHub and access tokens.
        </p>
      </div>

      <RunnersPanel />

      {/* Presence */}
      <div className="rounded-xl border border-border bg-card p-5 space-y-3">
        <h2 className="text-[10px] uppercase tracking-wider font-semibold text-muted-foreground">Presence</h2>
        <div className="flex items-start gap-3">
          <input
            id="presence-toggle"
            type="checkbox"
            checked={showPresence}
            onChange={(e) => void togglePresence(e.target.checked)}
            aria-describedby="presence-toggle-help"
            className="mt-0.5 h-4 w-4 rounded border-input text-primary focus:ring-primary"
          />
          <div>
            {/* The <label> holds ONLY the control's name — its content is
                what screen readers announce as the checkbox's accessible
                name. The help sentence lives in its own <p>, wired in via
                aria-describedby instead of nesting inside the label, so the
                accessible name stays "Show me as viewing" rather than the
                whole two-sentence blob. */}
            <label htmlFor="presence-toggle" className="block text-sm text-foreground-secondary cursor-pointer">
              Show me as viewing
            </label>
            <p id="presence-toggle-help" className="text-xs text-muted-foreground">
              When off, you can still see who else is viewing a page, but they cannot see you.
            </p>
          </div>
        </div>
      </div>

      <GitHubPanel />

      <TokensPanel />

    </div>
  )
}

/**
 * The runners you can administer — their Claude login lives on each box, not
 * here. Runners are owned by the person who paired them (plus anyone they
 * grant), so this lists exactly the boxes whose credentials are yours to fix,
 * each linking to its detail on Supervisor.
 */
function RunnersPanel() {
  const [runners, setRunners] = useState<RunnerOut[] | null>(null)

  useEffect(() => {
    listRunners()
      .then((rows) => setRunners(rows.filter((r) => r.can_administer)))
      .catch(() => setRunners([]))
  }, [])

  return (
    <div className="rounded-xl border border-border bg-card p-5 space-y-3" data-testid="settings-runners">
      <div>
        <h2 className="text-sm font-semibold text-foreground">Runners</h2>
        <p className="mt-1 text-sm text-muted-foreground">
          Agents run on runners, and each cloud runner holds its own Claude login — with a fallback
          subscription or an API key for when a weekly cap hits.
        </p>
      </div>
      {runners === null ? (
        <p className="text-sm text-muted-foreground">Loading…</p>
      ) : runners.length === 0 ? (
        <p className="text-sm text-muted-foreground">You don&apos;t administer any runners.</p>
      ) : (
        <table className="w-full text-[13px]">
          <tbody>
            {runners.map((r) => (
              <tr key={r.id} className="border-t border-border first:border-t-0">
                <td className="py-1.5 font-medium text-foreground">{r.name}</td>
                <td className="py-1.5 text-muted-foreground">{r.kind}</td>
                <td className="py-1.5 text-muted-foreground">{r.status}</td>
                <td className="py-1.5 text-right">
                  <Link
                    to={`/supervisor?tab=runners&runner=${r.id}`}
                    className="text-primary hover:underline"
                  >
                    {r.kind === 'cloud' ? 'Claude login & admins →' : 'Details →'}
                  </Link>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  )
}
