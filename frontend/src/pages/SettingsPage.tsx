import { useEffect, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { listRunners, type RunnerOut } from '@/api/harness'
import { listRunnerDeviceKeys, type RunnerDeviceKeyOut } from '@/api/deviceKeys'
import { DeviceKeysTable } from '@/components/settings/DeviceKeysTable'
import { getPresencePreference, setPresencePreference } from '@/api/presence'
import { notifyPresencePreferenceChanged } from '@/presence/events'
import { TokensPanel } from '@/components/settings/TokensPanel'
import { SecurityCard } from '@/components/settings/SecurityCard'
import { GitHubPanel } from '@/components/settings/GitHubPanel'
import { ConnectedAppsPanel } from '@/components/settings/ConnectedAppsPanel'
import { useMemoryVisible } from '@/auth/memoryRollout'
import { SessionWorkspacePanel } from '@/components/settings/SessionWorkspacePanel'

export function SettingsPage() {
  const memoryVisible = useMemoryVisible()
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
          Your account: the runners you administer, presence, security, GitHub, connected apps and access tokens.
        </p>
        {memoryVisible && (
          <p className="mt-1 text-xs">
            <Link to="/people/me/" className="text-primary underline-offset-2 hover:underline">
              Your memory
            </Link>
          </p>
        )}
      </div>

      <RunnersPanel />

      <SessionWorkspacePanel />

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

      <SecurityCard machines={<DeviceKeysPanel />} />

      <GitHubPanel />

      <ConnectedAppsPanel />

      <TokensPanel />

    </div>
  )
}

/**
 * Device keys on the runners you administer (Canopy Vault, canopy-web#1538). A
 * box registering a key prints `approve_url`, which lands here with
 * `?device_key=<id>`; that row is marked so the approver knows which one.
 */
function DeviceKeysPanel() {
  const [keys, setKeys] = useState<RunnerDeviceKeyOut[] | null>(null)
  const [params] = useSearchParams()
  const highlight = params.get('device_key')

  useEffect(() => {
    listRunnerDeviceKeys()
      .then(setKeys)
      .catch(() => setKeys([]))
  }, [])

  // A row of the Security card: the machines that may read agents' secrets.
  // A new one can read nothing until you approve it.
  return (
    <div id="device-keys" className="space-y-2" data-testid="settings-device-keys">
      {keys === null ? (
        <p className="text-sm text-muted-foreground">Loading…</p>
      ) : keys.length === 0 ? (
        <p className="text-sm text-muted-foreground">No machines yet. One shows up here when a runner asks to read secrets.</p>
      ) : (
        <DeviceKeysTable
          keys={keys}
          highlight={highlight}
          onChange={(next) => setKeys((ks) => (ks ?? []).map((k) => (k.id === next.id ? next : k)))}
        />
      )}
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
