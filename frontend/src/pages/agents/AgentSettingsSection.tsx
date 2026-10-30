import { useEffect, useState } from 'react'
import { Navigate, useLocation, useNavigate, useOutletContext, useParams, useSearchParams } from 'react-router-dom'
import { getAgentSetup } from '@/api/agentCreation'
import type { AgentOutletContext } from '@/pages/AgentWorkspacePage'
import { ObjectTabs } from '@/components/objectPage/ObjectTabs'
import {
  isSettingsTab,
  SETTINGS_TABS,
  tabForAnchor,
  tabForStepHref,
  type SettingsTab,
} from '@/pages/agents/settingsTabs'
import { WorkbenchSubHeader } from 'canopy-ui'
import { roleAllows } from '@/lib/workspaceRoles'
import { useWorkspaceRole } from '@/workspace/WorkspaceProvider'
import { AccountsTab } from '@/pages/agents/settings/AccountsTab'
import { ChannelsTab } from '@/pages/agents/settings/ChannelsTab'
import { OverviewTab } from '@/pages/agents/settings/OverviewTab'
import { RolesTab } from '@/pages/agents/settings/RolesTab'
import { SecurityTab } from '@/pages/agents/settings/SecurityTab'

// EVERYTHING THAT CONFIGURES AN AGENT, behind tabs (`settings/<tab>`).
//
// It was one long page; changing one key meant scrolling past every other
// setting, and Credentials alone had nine sub-panels. Each tab has its own
// address; old `settings#anchor` links forward to the tab holding the anchor
// (settingsTabs.ts), and the page scrolls to it there.
//
//   Overview           — what is left to set up, what it is, where it runs, delete
//   Roles              — owner, its canopy user, access (who has which role), who runs it directly
//   Channels           — capabilities, Slack, email, Google account
//   External accounts  — GitHub, its Google mailbox
//   Security           — its vault (secrets, who can open them, storage and key), then its canopy login
export function AgentSettingsSection() {
  const { agent, hold, setHold } = useOutletContext<AgentOutletContext>()
  const { tab } = useParams()
  const { hash, search, key } = useLocation()
  const navigate = useNavigate()
  const isAdmin = agent.is_admin ?? false
  const admins = `${agent.name}'s admins`
  // Routing writes are gated on agent.work (their @gate): workspace editors.
  const canRoute = roleAllows(useWorkspaceRole(agent.workspace), 'agent.work')
  // Set by the New agent page on its way here, so the person sees it worked.
  const created = useSearchParams()[0].get('created')

  // A tab with a setup step left gets a dot, so "what is left" is visible from
  // whichever tab you are on. The server decides what is left; this only maps
  // each step's link to the tab it opens.
  const [needs, setNeeds] = useState<Set<SettingsTab>>(new Set())
  useEffect(() => {
    let cancelled = false
    getAgentSetup(agent.slug)
      .then((steps) => {
        if (cancelled) return
        const open = steps.filter((s) => (s.status === 'todo' || s.status === 'waiting') && !s.only_to_run_on_its_own)
        setNeeds(new Set(open.map((s) => tabForStepHref(s.href)).filter((t): t is SettingsTab => t !== null)))
      })
      .catch(() => undefined)
    return () => {
      cancelled = true
    }
  }, [agent.slug])

  const current: SettingsTab | null = isSettingsTab(tab) ? tab : null
  const wanted = hash ? tabForAnchor(hash, search) : null

  // An anchor that lives on another tab (an in-page `#routing` link, a held
  // `#credentials` address) moves to that tab, keeping the anchor.
  useEffect(() => {
    if (current && wanted && wanted !== current) navigate(`../${wanted}${search}${hash}`, { replace: true, relative: 'path' })
  }, [current, wanted, search, hash, navigate])

  // The router does not scroll to a hash by itself. Keyed on the navigation
  // (`key`), not just the hash: clicking a link to the anchor already in the
  // address changes nothing else. And the target may render after its own data
  // loads, so wait for it (up to ~5s) instead of giving up on the first look.
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

  // `settings` itself, or a tab this page does not have: open the tab the
  // anchor belongs to, else Overview.
  if (!current) {
    const to = wanted ?? 'overview'
    return <Navigate to={`${tab ? '..' : '.'}/${to}${search}${hash}`} relative="path" replace />
  }

  return (
    <div className="max-w-5xl px-6 py-8">
      <WorkbenchSubHeader title="Settings" />

      <ObjectTabs
        label="Agent settings"
        relative="path"
        className="-mt-2 mb-6"
        tabs={SETTINGS_TABS.map((t) => ({ to: `../${t.tab}`, label: t.label, dot: needs.has(t.tab), dotLabel: 'needs setting up' }))}
      />

      {current === 'overview' && (
        <OverviewTab agent={agent} isAdmin={isAdmin} canRoute={canRoute} admins={admins} hold={hold}
                     setHold={setHold} created={created} />
      )}
      {current === 'roles' && <RolesTab agent={agent} isAdmin={isAdmin} admins={admins} />}
      {current === 'channels' && <ChannelsTab agent={agent} isAdmin={isAdmin} admins={admins} />}
      {current === 'accounts' && <AccountsTab agent={agent} isAdmin={isAdmin} />}
      {current === 'security' && <SecurityTab agent={agent} isAdmin={isAdmin} />}
    </div>
  )
}
