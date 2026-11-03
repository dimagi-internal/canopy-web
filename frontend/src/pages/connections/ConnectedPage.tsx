import { useEffect, useState, type JSX } from 'react'
import { Link, useParams, useSearchParams } from 'react-router-dom'
import { getShareState, type ShareStateOut } from '@/api/connections'
import { ApiError } from '@/api/problem'
import { scopedPath } from '@/lib/scopedLinks'
import { safeReturnTo } from './ShareConnectionPage'

const NOT_YOURS = "This connection isn't yours to share."
const CHOOSE = 'One step left: choose who can use it.'

const linkButton =
  'inline-flex items-center rounded bg-primary px-3 py-1.5 text-[13px] font-medium text-primary-foreground hover:bg-primary/90'

type Copy = { text: string; action: 'person' | 'agent' | 'choose'; agent: string }

/** What to say, from what the page can read without opening anything. Only `server.reachable`
 *  (the stored sign-in is sealed to canopy-web's key now) lets it say screens can use it:
 *  `server.on` alone is only the switch, and a sign-in made while the key service was failing
 *  leaves it on with nothing sealed (false). Null: the key can't be read right now, which needs
 *  nothing from anyone, so the page goes on as if it could. */
export function copyFor(s: ShareStateOut): Copy {
  const account = s.account_label || s.account_id
  const agent = s.owner_agent ? s.owner_agent.name || s.owner_agent.slug : ''
  const calls = !!s.server?.provider_calls
  if (calls && s.server?.reachable) {
    return agent
      ? {
          text: `Connected ${agent} as ${account}. Screens can use it now. One step left for ${agent}'s own runs:`,
          action: 'agent', agent,
        }
      : { text: `Connected as ${account}. Project screens and apps you use can use it now.`, action: 'person', agent }
  }
  if (calls && s.server?.on && s.server.reachable === null) {
    return {
      text: `Connected${agent ? ` ${agent}` : ''} as ${account}. canopy-web can't be reached right now, so project screens and apps may not work for a moment.`,
      action: agent ? 'agent' : 'person', agent,
    }
  }
  if (calls && s.server?.on) {
    return {
      text: `Connected as ${account}. canopy-web couldn't be reached to turn on project screens and apps. ${CHOOSE}`,
      action: 'choose', agent,
    }
  }
  return { text: `Connected as ${account}. ${CHOOSE}`, action: 'choose', agent }
}

/**
 * "Connected" (`/w/:workspace/connections/:id/connected`): where a successful sign-in lands. It
 * says plainly what was connected and who can use it now, and links on to the Share screen for
 * the rest. It reads `share_state` (ciphertext and facts) but never unlocks anything: no passkey,
 * no vault prompt. Done goes back to where the person started (`return_to`).
 */
export function ConnectedPage(): JSX.Element | null {
  const { workspace = '', id = '' } = useParams()
  const [params] = useSearchParams()
  const returnTo = safeReturnTo(params.get('return_to'))
  const [state, setState] = useState<ShareStateOut | null>(null)
  // The owner's key may not be this browser's (an admin who connected for an agent): the
  // connection is still made, so say so without the details only the share state carries.
  const [unreadable, setUnreadable] = useState(false)
  const [notYours, setNotYours] = useState(false)
  const [loadError, setLoadError] = useState('')

  useEffect(() => {
    let cancelled = false
    getShareState(id)
      .then((s) => !cancelled && setState(s))
      .catch((e: unknown) => {
        if (cancelled) return
        if (e instanceof ApiError && e.status === 404) setNotYours(true)
        else if (e instanceof ApiError && e.status === 403) setUnreadable(true)
        else setLoadError(e instanceof Error ? e.message : 'Could not read this connection.')
      })
    return () => { cancelled = true }
  }, [id])

  const share = `${scopedPath(workspace, `/connections/${id}/share`)}?${new URLSearchParams({ return_to: returnTo })}`

  if (notYours) {
    return (
      <div className="max-w-2xl space-y-2">
        <p role="alert" className="text-sm text-destructive">{NOT_YOURS}</p>
        <Link to={returnTo} className="text-sm text-primary underline">Back</Link>
      </div>
    )
  }
  if (loadError) return <p className="max-w-2xl text-sm text-destructive">{loadError}</p>
  if (!state && !unreadable) return <div className="h-32 max-w-2xl animate-pulse rounded-lg bg-muted" />

  const copy: Copy = state ? copyFor(state) : { text: `Connected. ${CHOOSE}`, action: 'choose', agent: '' }
  return (
    <div className="max-w-2xl space-y-4" data-testid="connected">
      <h1 className="text-lg font-semibold text-foreground">Connected</h1>
      <div className="space-y-3 rounded border border-success/30 bg-success/10 p-3 text-[13px] text-success">
        <p role="status">{copy.text}</p>
      </div>
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
        {copy.action === 'person' && (
          <>
            <Link to={share} className="text-sm text-primary hover:underline">Also let agents or my computers use it</Link>
            <Link to={returnTo} className={linkButton}>Done</Link>
          </>
        )}
        {copy.action === 'agent' && <Link to={share} className={linkButton}>{`Turn on for ${copy.agent}'s machines`}</Link>}
        {copy.action === 'choose' && <Link to={share} className={linkButton}>Choose</Link>}
        {copy.action !== 'person' && copy.agent && <Link to={returnTo} className="text-sm text-primary hover:underline">Done</Link>}
      </div>
    </div>
  )
}
