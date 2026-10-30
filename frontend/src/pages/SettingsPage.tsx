import { useEffect, useState } from 'react'
import { Link, Navigate, useLocation, useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { WorkbenchSubHeader } from 'canopy-ui'
import { listRunners, type RunnerOut } from '@/api/harness'
import { listRunnerDeviceKeys, type RunnerDeviceKeyOut } from '@/api/deviceKeys'
import { DeviceKeysTable } from '@/components/settings/DeviceKeysTable'
import { apiV2 } from '@/api/client.v2'
import { getPresencePreference, setPresencePreference } from '@/api/presence'
import { notifyPresencePreferenceChanged } from '@/presence/events'
import { TokensPanel } from '@/components/settings/TokensPanel'
import { SecurityCard } from '@/components/settings/SecurityCard'
import { ContactDetailsPanel } from '@/components/settings/ContactDetailsPanel'
import { ConnectionsPanel } from '@/components/settings/ConnectionsPanel'
import { GitHubPanel } from '@/components/settings/GitHubPanel'
import { ConnectedAppsPanel } from '@/components/settings/ConnectedAppsPanel'
import { ActAsYouPanel } from '@/components/settings/ActAsYouPanel'
import { DelegatedCredentialsPanel } from '@/components/settings/DelegatedCredentialsPanel'
import { SessionWorkspacePanel } from '@/components/settings/SessionWorkspacePanel'
import { CanopyProjectsPanel } from '@/components/settings/CanopyProjectsPanel'
import { RunnerAppPanel } from '@/components/settings/RunnerAppPanel'
import { SessionReviewersPanel } from '@/components/settings/SessionReviewersPanel'
import { AgentLoginRequestsPanel } from '@/components/settings/AgentLoginRequestsPanel'
import { runnerHref } from '@/lib/scopedLinks'
import { ProfileSection } from '@/pages/SettingsLayout'
import {
  SETTINGS_SECTIONS,
  isSettingsSection,
  sectionForAddress,
  type SettingsSection,
} from '@/pages/settingsSections'

// ONE SECTION OF SETTINGS (`/settings/<section>`), beside the rail that lists
// them all (SettingsLayout).
//
// It was one long two-column page of fourteen cards; finding one meant reading
// all of them. Each section has its own address; bare `/settings` opens
// Profile, and old `/settings#anchor` and `/settings?device_key=…` / `?github=…`
// links forward to the section holding them (settingsSections.ts), and the page
// scrolls to the anchor there.
//
//   Profile               — your workspaces, and how members of each see you
//   Account               — contact details, presence, where sessions are filed, who reviews them
//   Security              — passkeys, recovery, runners that may open secrets, your agents
//   Agents acting for you — credentials you delegated, act-as-you permissions
//   Runners               — runners you administer, the repos you use canopy in, the runner app
//   Connections           — GitHub, MCP clients, personal access tokens
export function SettingsPage() {
  const { section } = useParams()
  const { hash, search, key } = useLocation()
  const current: SettingsSection | null = isSettingsSection(section) ? section : null
  const wanted = hash || search ? sectionForAddress(hash, search) : null

  // An anchor that lives in another section (an in-page link, a held address)
  // moves to that section, keeping the anchor and the query.
  const navigate = useNavigate()
  useEffect(() => {
    if (current && wanted && wanted !== current) navigate(`/settings/${wanted}${search}${hash}`, { replace: true })
  }, [current, wanted, search, hash, navigate])

  // The router does not scroll to a hash by itself, and the target may render
  // only after its own data loads — look for it for up to ~5s.
  useEffect(() => {
    if (!hash) return
    const id = decodeURIComponent(hash.slice(1))
    let tries = 0
    const look = (): boolean => {
      const el = document.getElementById(id)
      el?.scrollIntoView?.({ block: 'start' })
      return Boolean(el) || ++tries >= 50
    }
    if (look()) return
    const timer = window.setInterval(() => {
      if (look()) window.clearInterval(timer)
    }, 100)
    return () => window.clearInterval(timer)
  }, [hash, key])

  // Bare `/settings`, or a section this page does not have: open the section the
  // old address belongs to, else Profile.
  if (!current) return <Navigate to={`/settings/${wanted ?? 'profile'}${search}${hash}`} replace />
  const title = SETTINGS_SECTIONS.find((s) => s.section === current)?.label ?? 'Settings'

  return (
    <div className="max-w-4xl px-6 py-8">
      <WorkbenchSubHeader title={title} />

      {/* An agent asking to sign in as you waits on you whichever section is
          open; it renders nothing when none is. */}
      <div className="mb-5 empty:hidden">
        <AgentLoginRequestsPanel />
      </div>

      <div className="space-y-5">
        {current === 'profile' && <ProfileSection />}
        {current === 'account' && (
          <>
            <ContactDetailsPanel />
            <PresencePanel />
            <ApprovalCardsPanel />
            <SessionWorkspacePanel />
            <SessionReviewersPanel />
          </>
        )}
        {current === 'security' && <SecurityCard machines={<DeviceKeysPanel />} />}
        {current === 'agents' && (
          <>
            <DelegatedCredentialsPanel />
            <ActAsYouPanel />
          </>
        )}
        {current === 'runners' && (
          <>
            <RunnersPanel />
            <CanopyProjectsPanel />
            <RunnerAppPanel />
          </>
        )}
        {current === 'connections' && (
          <>
            <GitHubPanel />
            <ConnectionsPanel />
            <ConnectedAppsPanel />
            <TokensPanel />
          </>
        )}
      </div>
    </div>
  )
}

/** Whether others see you on the page they are viewing. */
/**
 * Approval cards: a proposal waiting on your yes, sent to you with everything needed to decide it
 * (a notification, a Slack message, or an email when neither reaches you). Off leaves them on the
 * board and Home only.
 */
function ApprovalCardsPanel() {
  const [on, setOn] = useState<boolean | null>(null)
  useEffect(() => {
    let cancelled = false
    // A preference that fails to load just leaves the panel out; it is not worth an error.
    void apiV2
      .GET('/api/push/preferences')
      .then(({ data }) => {
        if (!cancelled && data) setOn(data.approval_cards)
      })
      .catch(() => undefined)
    return () => {
      cancelled = true
    }
  }, [])
  if (on === null) return null
  const toggle = (next: boolean) => {
    setOn(next)
    void apiV2
      .PATCH('/api/push/preferences', { body: { approval_cards: next } })
      .then(({ data }) => (data ? setOn(data.approval_cards) : setOn(!next)))
      .catch(() => setOn(!next))
  }
  return (
    <div className="rounded-xl border border-border bg-card p-5 space-y-3">
      <h2 className="text-sm font-semibold text-foreground">Approval cards</h2>
      <div className="flex items-start gap-3">
        <input
          id="approval-cards-toggle"
          type="checkbox"
          checked={on}
          onChange={(e) => toggle(e.target.checked)}
          aria-describedby="approval-cards-help"
          className="mt-0.5 h-4 w-4 rounded border-input text-primary focus:ring-primary"
        />
        <div>
          <label htmlFor="approval-cards-toggle" className="block text-sm text-foreground-secondary cursor-pointer">
            Send me approval cards
          </label>
          <p id="approval-cards-help" className="text-xs text-muted-foreground">
            A proposal waiting on your yes arrives as a notification, a Slack message with Approve and Decline
            buttons, or an email when neither reaches you — with everything needed to decide. When off, they
            stay on the board and Home.
          </p>
        </div>
      </div>
    </div>
  )
}

function PresencePanel() {
  // Defaults to visible (matches the backend default when no preference row
  // exists yet) so the checkbox never flashes unchecked before the fetch resolves.
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

  return (
    <div className="rounded-xl border border-border bg-card p-5 space-y-3">
      <h2 className="text-sm font-semibold text-foreground">Presence</h2>
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
        <p className="text-sm text-muted-foreground">No runners yet. One shows up here when a runner asks to read secrets.</p>
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
                    to={runnerHref(r)}
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
