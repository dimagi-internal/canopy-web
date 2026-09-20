import type { JSX } from 'react'
import { Link, NavLink, Outlet, useParams } from 'react-router-dom'
import { clsx } from 'clsx'
import { useWorkspace } from '@/workspace/WorkspaceProvider'

/**
 * One settings surface per workspace.
 *
 * Members, Slack, inbound email and connected sites were four sibling
 * top-level routes with no home tying them together — so "where do I change
 * something about this workspace" had four answers, three of them in the nav
 * and one (connected sites) in no menu at all. They are one thing: how this
 * workspace is configured.
 *
 * They are SECTIONS BEHIND TABS rather than stacked onto one scrolling page,
 * for two reasons. Each fetches its own data (members + invites, the Slack
 * config, the push config + mailboxes + agents, the app list + agents), so
 * stacking them would make every visit pay for all four. And the Slack section
 * holds a one-way action — declaring the Slack app an agent, which Slack
 * cannot undo — which belongs on a section you navigated to deliberately, not
 * halfway down a long page you were scrolling through for something else.
 *
 * Each section keeps its own heading: the tab says where you are, the heading
 * says what the section is (and carries its count, where it has one).
 */
export interface SettingsSection {
  /** Path segment under /w/:workspace/settings. */
  segment: string
  label: string
}

/** The tab row, in the order it renders. `members` is also the index target —
 *  it is the section every workspace has something to say about. */
export const SETTINGS_SECTIONS: SettingsSection[] = [
  { segment: 'members', label: 'Members' },
  { segment: 'slack', label: 'Slack' },
  { segment: 'inbound', label: 'Inbound email' },
  { segment: 'connected-apps', label: 'Connected sites' },
]

export function WorkspaceSettingsPage(): JSX.Element {
  const { workspace: slug = '' } = useParams()
  const { workspaces } = useWorkspace()
  const name = workspaces.find((w) => w.slug === slug)?.display_name

  // No page padding here: AppLayout's <main> already applies the gutter, and a
  // page adding its own on top is what once spent a quarter of a phone's width
  // before any content (see AppLayout).
  return (
    <div>
      <header className="mb-5 max-w-4xl">
        <h1 className="text-lg font-semibold text-foreground">
          Settings{name ? <span className="text-muted-foreground"> · {name}</span> : null}
        </h1>
        <p className="mt-1 text-[13px] text-foreground-secondary">
          How this workspace is configured — who is in it, and how work reaches it. Your own
          account settings (AI backend, tokens, theme) live in{' '}
          <Link to="/settings" className="text-primary hover:underline">
            your settings
          </Link>
          .
        </p>
      </header>

      <nav aria-label="Workspace settings" className="mb-6 flex gap-1 overflow-x-auto border-b border-border">
        {SETTINGS_SECTIONS.map((section) => (
          <NavLink
            key={section.segment}
            to={section.segment}
            className={({ isActive }) =>
              clsx(
                'shrink-0 border-b-2 px-3 py-2 text-[13px] transition-colors',
                isActive
                  ? 'border-primary text-foreground font-medium'
                  : 'border-transparent text-muted-foreground hover:text-foreground-secondary',
              )
            }
          >
            {section.label}
          </NavLink>
        ))}
      </nav>

      <Outlet />
    </div>
  )
}
