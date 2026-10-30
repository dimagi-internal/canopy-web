// The pieces of the history page: the credential notice and the play, pause and chevron icons. The page and its state are `AgentHistorySection`.
import { type ReactNode, } from 'react'
import { Link, } from 'react-router-dom'
import { type SkillHistoryOut } from '@/api/agents'

/** Why there is no (fresh) history, and who can do something about it. The
 *  credential is the agent OWNER's, so the copy names them, offers Connect only
 *  to them, and links GitHub's installation screen when the repo isn't granted. */
export function CredentialNotice({ h }: { h: SkillHistoryOut }) {
  const owner = h.owner_name || 'The agent owner'
  const ownerPossessive = h.owner_name ? `${h.owner_name}’s` : 'the agent owner’s'
  const ownerPossessiveStart = h.owner_name ? `${h.owner_name}’s` : 'The agent owner’s'
  let body: ReactNode = null
  switch (h.credential_state) {
    case 'no_repo':
      body = 'This agent has no repository configured, so there is no history to read.'
      break
    case 'no_owner':
      body = <>History reads through the agent owner’s GitHub connection, and this agent has no owner. A workspace owner can assign one on <Link to="../overview" className="underline">Overview</Link>.</>
      break
    case 'owner_not_connected':
      body = h.viewer_is_owner
        ? <>History reads through your GitHub connection, and you haven’t connected GitHub. <Link to="/settings" className="underline">Connect GitHub</Link></>
        : <>History reads through {ownerPossessive} GitHub connection. {owner} hasn’t connected GitHub, and needs to connect it in their canopy settings.</>
      break
    case 'repo_not_granted':
      body = (
        <>
          {h.viewer_is_owner ? 'Your' : ownerPossessiveStart} GitHub connection can’t reach{' '}
          <span className="font-mono">{h.repo_url || 'this agent’s repository'}</span>.
          {' '}{h.viewer_is_owner ? 'You can add it' : `${owner} can add it`} on GitHub’s installation screen.
          {h.install_url && (
            <>
              {' '}
              <a href={h.install_url} target="_blank" rel="noreferrer" className="underline">Choose repositories on GitHub</a>
            </>
          )}
        </>
      )
      break
    default:
      return null
  }
  return (
    <p role="status" className="m-0 rounded-lg border border-warning/30 bg-warning/10 px-3 py-2 text-[13px] text-warning">
      {body}
    </p>
  )
}

// Small inline stroke icons — not text glyphs (▶/❚❚ render as emoji on some
// platforms, and this app forbids emoji in UI copy).
export function PlayIcon() {
  return (
    <svg viewBox="0 0 24 24" className="h-4 w-4" fill="currentColor" aria-hidden="true">
      <path d="M7 4.5v15l13-7.5z" />
    </svg>
  )
}

export function PauseIcon() {
  return (
    <svg viewBox="0 0 24 24" className="h-4 w-4" fill="currentColor" aria-hidden="true">
      <rect x="6" y="4.5" width="4.5" height="15" />
      <rect x="13.5" y="4.5" width="4.5" height="15" />
    </svg>
  )
}

export function ChevronLeftIcon() {
  return (
    <svg viewBox="0 0 24 24" className="h-4 w-4" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <polyline points="15 6 9 12 15 18" />
    </svg>
  )
}

export function ChevronRightIcon() {
  return (
    <svg viewBox="0 0 24 24" className="h-4 w-4" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <polyline points="9 6 15 12 9 18" />
    </svg>
  )
}
