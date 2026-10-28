import type { JSX } from 'react'
import { Link, NavLink, Outlet } from 'react-router-dom'
import { WorkbenchMain, WorkbenchNavItem, WorkbenchRail, WorkbenchShell, WorkbenchSubHeader } from 'canopy-ui'

import { useAuth } from '@/auth/AuthProvider'
import { useMemoryVisible } from '@/auth/memoryRollout'
import { useWorkspace } from '@/workspace/WorkspaceProvider'
import { scopedPath } from '@/lib/scopedLinks'

/**
 * Your own page: one frame over the pages that are about YOU — who you are and
 * where you are a member (Profile), what agents know about you (Memory), and
 * your account (Settings, itself tabbed). The same shape as an agent's page:
 * who it is and its sections down a left rail, a section's own tabs across the
 * top. Memory and Settings keep the addresses they have always had
 * (/people/me, /settings), so every link already handed out still lands.
 */
export function MeLayout(): JSX.Element {
  return (
    <WorkbenchShell>
      <MeLeftNav />
      <WorkbenchMain>
        <Outlet />
      </WorkbenchMain>
    </WorkbenchShell>
  )
}

function MeLeftNav(): JSX.Element {
  const auth = useAuth()
  const memoryVisible = useMemoryVisible()
  const me = auth.status === 'authenticated' ? auth.user : null
  const items = [
    { to: '/me', label: 'Profile', end: true },
    // Memory is shown only to the people it is on for (memoryRollout.ts).
    ...(memoryVisible ? [{ to: '/people/me', label: 'Memory', end: false }] : []),
    { to: '/settings', label: 'Settings', end: false },
  ]

  const header = (
    <div className="flex items-start gap-3 px-4 py-4">
      <Avatar me={me} size="h-10 w-10 text-sm" />
      <div className="min-w-0">
        <h2 className="truncate text-sm font-semibold leading-snug text-foreground">{me?.name || 'You'}</h2>
        {me?.email && <div className="truncate text-[11px] text-muted-foreground">{me.email}</div>}
      </div>
    </div>
  )

  return (
    <WorkbenchRail header={header}>
      {/* Phone: a horizontal strip, as on an agent's page; a rail from md up. */}
      <nav aria-label="Your profile" className="px-2 py-3">
        <div className="flex gap-0.5 overflow-x-auto md:flex-col md:overflow-x-visible">
          {items.map((item) => (
            <NavLink key={item.to} to={item.to} end={item.end} className="shrink-0 md:shrink">
              {({ isActive }) => <WorkbenchNavItem active={isActive}>{item.label}</WorkbenchNavItem>}
            </NavLink>
          ))}
        </div>
      </nav>
    </WorkbenchRail>
  )
}

function Avatar({ me, size }: { me: { name: string; email: string; avatar_url?: string | null } | null; size: string }) {
  if (me?.avatar_url) return <img src={me.avatar_url} alt="" className={`${size} shrink-0 rounded-full`} />
  return (
    <span className={`flex ${size} shrink-0 items-center justify-center rounded-full bg-primary/10 font-semibold text-primary`}>
      {(me?.name || me?.email || '?').charAt(0).toUpperCase()}
    </span>
  )
}

/** The Profile tab: who you are, and each workspace you are in — with the link to
 *  the page other members there see for you. */
export function MeProfileTab(): JSX.Element | null {
  const auth = useAuth()
  const { workspaces } = useWorkspace()
  if (auth.status !== 'authenticated') return null
  const me = auth.user

  return (
    <div className="max-w-4xl px-6 py-8">
      <WorkbenchSubHeader title="Profile" />

      <section>
        <h2 className="mb-2 text-sm font-semibold text-foreground">Your workspaces</h2>
        {workspaces.length === 0 ? (
          <p className="text-[13px] text-muted-foreground">You are not in a workspace yet.</p>
        ) : (
          <table className="w-full text-[13px]">
            <thead>
              <tr className="border-b border-border text-left text-[11px] uppercase tracking-wider text-muted-foreground">
                <th className="py-2 pr-4 font-medium">Workspace</th>
                <th className="py-2 pr-4 font-medium">Your role</th>
                <th className="py-2" />
              </tr>
            </thead>
            <tbody>
              {workspaces.map((w) => (
                <tr key={w.slug} className="border-b border-border last:border-b-0">
                  <td className="py-2 pr-4">
                    <Link to={scopedPath(w.slug, '')} className="font-medium text-foreground hover:text-primary">
                      {w.display_name || w.slug}
                    </Link>
                  </td>
                  <td className="py-2 pr-4 capitalize text-muted-foreground">{w.role}</td>
                  <td className="py-2 text-right">
                    {me.user_id != null && (
                      <Link to={scopedPath(w.slug, `/members/${me.user_id}`)} className="text-primary hover:underline">
                        How members see you →
                      </Link>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
    </div>
  )
}
