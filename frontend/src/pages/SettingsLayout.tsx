import { useEffect, useRef, type JSX } from 'react'
import { Link, NavLink, Outlet, useLocation } from 'react-router-dom'
import { WorkbenchMain, WorkbenchNavItem, WorkbenchRail, WorkbenchShell } from 'canopy-ui'

import { useAuth } from '@/auth/AuthProvider'
import { useMemoryVisible } from '@/auth/memoryRollout'
import { useWorkspace } from '@/workspace/WorkspaceProvider'
import { scopedPath } from '@/lib/scopedLinks'
import { SETTINGS_SECTIONS } from '@/pages/settingsSections'

/**
 * Settings: one frame over everything about YOU, the same in every workspace —
 * who you are and where you are a member (Profile), your account's settings in
 * sections, and what agents know about you (Memory). Every section is one entry
 * down the left rail, one click away; no section has tabs of its own.
 * Memory keeps the address it has always had (/people/me), and /me forwards to
 * Profile, so every link already handed out still lands.
 */
export function SettingsLayout(): JSX.Element {
  return (
    <WorkbenchShell>
      <SettingsLeftNav />
      <WorkbenchMain>
        <Outlet />
      </WorkbenchMain>
    </WorkbenchShell>
  )
}

function SettingsLeftNav(): JSX.Element {
  const auth = useAuth()
  const memoryVisible = useMemoryVisible()
  const me = auth.status === 'authenticated' ? auth.user : null
  const items = [
    ...SETTINGS_SECTIONS.map((s) => ({ to: `/settings/${s.section}`, label: s.label })),
    // Memory is shown only to the people it is on for (memoryRollout.ts).
    ...(memoryVisible ? [{ to: '/people/me', label: 'Memory' }] : []),
  ]

  // On a phone the strip is narrower than its entries, so bring the open one
  // into view (centred). Only the strip scrolls: scrollIntoView would move the
  // page too. From md up the rail never overflows and this does nothing.
  const stripRef = useRef<HTMLDivElement>(null)
  const { pathname } = useLocation()
  useEffect(() => {
    const strip = stripRef.current
    const active = strip?.querySelector<HTMLElement>('[aria-current="page"]')
    if (!strip || !active || strip.scrollWidth <= strip.clientWidth) return
    const s = strip.getBoundingClientRect()
    const a = active.getBoundingClientRect()
    strip.scrollLeft += a.left - s.left - (s.width - a.width) / 2
  }, [pathname, items.length])

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
      {/* Phone: a horizontal strip, as on an agent's page; a rail from md up.
          The strip scrolls, so its right edge fades to say so. */}
      <nav
        aria-label="Settings"
        className="relative px-2 py-3 after:pointer-events-none after:absolute after:inset-y-3 after:right-0 after:w-10 after:bg-gradient-to-l after:from-background after:via-background/80 after:to-transparent md:after:hidden"
      >
        <div ref={stripRef} className="flex gap-0.5 overflow-x-auto md:flex-col md:overflow-x-visible">
          {items.map((item) => (
            <NavLink key={item.to} to={item.to} className="shrink-0 md:shrink">
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

/** Profile: each workspace you are in, your role there, and the page other
 *  members there see for you. Who you are heads the rail beside it. */
export function ProfileSection(): JSX.Element | null {
  const auth = useAuth()
  const { workspaces } = useWorkspace()
  if (auth.status !== 'authenticated') return null
  const me = auth.user

  return (
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
  )
}
