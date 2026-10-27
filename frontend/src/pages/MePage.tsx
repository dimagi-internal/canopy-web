import type { JSX } from 'react'
import { Link, Outlet } from 'react-router-dom'

import { ObjectPage } from '@/components/objectPage/ObjectPage'
import { useAuth } from '@/auth/AuthProvider'
import { useWorkspace } from '@/workspace/WorkspaceProvider'
import { scopedPath } from '@/lib/scopedLinks'

/**
 * Your own page: one frame over the pages that are about YOU — who you are and
 * where you are a member (Profile), what agents know about you (Memory), and
 * your account (Settings). The Memory and Settings tabs keep the addresses they
 * have always had (/people/me, /settings), so every link already handed out
 * still lands, now inside this frame.
 */
export const ME_TABS = [
  { to: '/me', label: 'Profile', end: true },
  { to: '/people/me', label: 'Memory' },
  { to: '/settings', label: 'Settings' },
]

export function MeLayout(): JSX.Element {
  const auth = useAuth()
  const name = auth.status === 'authenticated' ? auth.user.name : undefined
  return (
    <ObjectPage title={name || 'You'} context="Your profile" tabsLabel="Your profile" tabs={ME_TABS} wide>
      <Outlet />
    </ObjectPage>
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
    <div className="max-w-3xl space-y-6">
      <section className="flex items-center gap-4 rounded-lg border border-border bg-card p-5">
        {me.avatar_url ? (
          <img src={me.avatar_url} alt="" className="h-14 w-14 rounded-full" />
        ) : (
          <span className="flex h-14 w-14 items-center justify-center rounded-full bg-primary/10 text-xl font-semibold text-primary">
            {(me.name || me.email).charAt(0).toUpperCase()}
          </span>
        )}
        <div className="min-w-0">
          <div className="text-base font-semibold text-foreground">{me.name}</div>
          <div className="text-[13px] text-muted-foreground">{me.email}</div>
        </div>
      </section>

      <section>
        <h2 className="mb-2 text-sm font-semibold text-foreground">Your workspaces</h2>
        {workspaces.length === 0 ? (
          <p className="text-[13px] text-muted-foreground">You are not in a workspace yet.</p>
        ) : (
          <ul className="divide-y divide-border rounded-lg border border-border">
            {workspaces.map((w) => (
              <li key={w.slug} className="flex items-center gap-3 px-4 py-3 text-[13px]">
                <Link to={scopedPath(w.slug, '')} className="font-medium text-foreground hover:text-primary">
                  {w.display_name || w.slug}
                </Link>
                <span className="text-muted-foreground capitalize">{w.role}</span>
                {me.user_id != null && (
                  <Link
                    to={scopedPath(w.slug, `/members/${me.user_id}`)}
                    className="ml-auto text-primary hover:underline"
                  >
                    How members see you →
                  </Link>
                )}
              </li>
            ))}
          </ul>
        )}
      </section>

      <section className="grid gap-3 sm:grid-cols-2">
        <Link to="/people/me" className="rounded-lg border border-border bg-card p-4 hover:border-primary/50">
          <div className="text-sm font-semibold text-foreground">Memory</div>
          <p className="mt-1 text-[13px] text-muted-foreground">What agents know about you, who can read it, and every change.</p>
        </Link>
        <Link to="/settings" className="rounded-lg border border-border bg-card p-4 hover:border-primary/50">
          <div className="text-sm font-semibold text-foreground">Settings</div>
          <p className="mt-1 text-[13px] text-muted-foreground">Your contact details, runners, tokens, connected apps and presence.</p>
        </Link>
      </section>
    </div>
  )
}
