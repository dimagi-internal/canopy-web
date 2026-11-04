import { useEffect, useRef, useState, type JSX } from 'react'
import { Link, useParams, useSearchParams } from 'react-router-dom'
import { getShareState, type ShareStateOut } from '@/api/connections'
import { ApiError } from '@/api/problem'
import { scopedPath } from '@/lib/scopedLinks'
import { Button } from 'canopy-ui/ui'
import { ShareRefused } from '@/vault/connectionShare'
import { autoChoice, openOwned, ownerAgentMachines, promptDismissed, putOwnerShare, reviewShare } from '@/vault/ownerShare'
import { useVaultUnlock } from '@/vault/useVaultUnlock'
import { safeReturnTo } from './ShareConnectionPage'

const NOT_YOURS = "This connection isn't yours to share."

const linkButton =
  'inline-flex items-center rounded bg-primary px-3 py-1.5 text-[13px] font-medium text-primary-foreground hover:bg-primary/90'
const quietLink = 'text-sm text-primary hover:underline'

/** `agent` is an agent's own connection: the page stores it for that agent (see ConnectedPage).
 *  `person`: the callback already sealed it to the person's vault key, so there is nothing to do. */
type Copy = { text: string; action: 'person' | 'agent'; agent: string; account: string; extra: string }

const KMS_FAILED = "canopy-web couldn't be reached to turn on project screens and apps."
const KMS_LATER = "canopy-web can't be reached right now, so project screens and apps may not work for a moment."

/** What to say, from what the page can read without opening anything. Only `server.reachable`
 *  (the stored sign-in is sealed to canopy-web's key now) lets it say screens can use it:
 *  `server.on` alone is only the switch, and a sign-in made while the key service was failing
 *  leaves it on with nothing sealed (false). Null: the key can't be read right now, which needs
 *  nothing from anyone, so the page goes on as if it could. */
export function copyFor(s: ShareStateOut): Copy {
  const account = s.account_label || s.account_id
  const agent = s.owner_agent ? s.owner_agent.name || s.owner_agent.slug : ''
  const calls = !!s.server?.provider_calls
  const action = agent ? 'agent' : 'person'
  const base = { action, agent, account } as const
  if (calls && s.server?.reachable) {
    return { ...base, extra: '', text: `Connected as ${account} and stored in your vault. Project screens and apps you use can use it now.` }
  }
  if (calls && s.server?.on && s.server.reachable === null) {
    return { ...base, extra: KMS_LATER, text: `Connected as ${account}. ${KMS_LATER}` }
  }
  if (calls && s.server?.on) {
    return { ...base, extra: KMS_FAILED, text: `Connected as ${account}. ${KMS_FAILED}` }
  }
  return { ...base, extra: '', text: `Connected as ${account} and stored in your vault. Nothing uses it yet.` }
}

/**
 * "Connected" (`/w/:workspace/connections/:id/connected`): where a successful sign-in lands. It
 * says plainly what was connected and who can use it now.
 *
 * A person's connection was sealed to their own vault key by the callback: nothing to do, no
 * prompt; Done goes back to where they started (`return_to`), and a quiet link goes on to Share.
 *
 * An agent's connection that is still unshared is stored in that agent's vault right here, with
 * no chooser: on arrival (`just_connected=1`, which the callback adds and this page strips, so
 * a reload or a shared link never prompts) it opens the owner's vault key and shares with that
 * agent alone, through the same functions as the Share screen (`vault/ownerShare`). If the
 * browser wants a click for the passkey, or it fails, a button does the same; a question only
 * a person can answer goes to the Share screen.
 */
export function ConnectedPage(): JSX.Element | null {
  const { workspace = '', id = '' } = useParams()
  const [params, setParams] = useSearchParams()
  const returnTo = safeReturnTo(params.get('return_to'))
  const justConnected = useRef(params.get('just_connected') === '1')
  const started = useRef(false)
  const vault = useVaultUnlock()
  const [state, setState] = useState<ShareStateOut | null>(null)
  // The owner's key may not be this browser's (an admin who connected for an agent): the
  // connection is still made, so say so without the details only the share state carries.
  const [unreadable, setUnreadable] = useState(false)
  const [notYours, setNotYours] = useState(false)
  const [loadError, setLoadError] = useState('')
  const [phase, setPhase] = useState<'idle' | 'storing' | 'stored' | 'click' | 'ask' | 'none'>('idle')
  // canopy-web was named in what was just stored, so a stale "couldn't be reached" no longer applies.
  const [webSealed, setWebSealed] = useState(false)
  const [error, setError] = useState('')

  // Read once, then taken off the address: only the sign-in's own landing may prompt.
  useEffect(() => {
    if (!params.has('just_connected')) return
    const next = new URLSearchParams(params)
    next.delete('just_connected')
    setParams(next, { replace: true })
  }, [params, setParams])

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

  /** Open the connection and store it for the agent. `auto` is the arrival try: it never opens the
   *  passphrase dialog by itself. The button reads the row again first, so a retry never reuses a
   *  stale one; a stale statement is read again and tried once more on its own. */
  async function store(first: ShareStateOut, auto: boolean) {
    setPhase('storing')
    setError('')
    try {
      let s = first
      if (!auto) {
        s = await getShareState(id)
        setState(s)
      }
      const key = await vault.unlock({ passkeyOnly: auto })
      let retried = false
      let choice
      for (;;) {
        try {
          const opened = await openOwned(s, key)
          const picked = autoChoice(s, opened.statement)
          if (picked.kind === 'ask') return setPhase('ask')
          choice = picked.choice
          const verdict = reviewShare(s, choice, opened.trust, opened.statement)
          if (verdict !== 'ready') return setPhase(verdict)
          await putOwnerShare(s, key, choice, opened.trust)
          break
        } catch (e) {
          if (retried || !(e instanceof ApiError && (e.code === 'statement_stale' || e.code === 'zk_value_changed'))) throw e
          retried = true
          s = await getShareState(id)
          setState(s)
        }
      }
      setWebSealed(!!choice.canopyWeb)
      try {
        setState(await getShareState(id))
      } catch {
        // what was stored stands; the sentence below only loses its latest facts
      }
      setPhase('stored')
    } catch (e) {
      if (e instanceof ShareRefused) return setPhase('ask')
      setPhase('click')
      if (!promptDismissed(e)) setError(e instanceof Error ? e.message : 'Could not store it.')
    }
  }

  const waiting = !!state?.owner_agent && state.state === 'unshared'
  useEffect(() => {
    if (!state || !waiting || !justConnected.current || started.current) return
    started.current = true
    void store(state, true)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [state, waiting])

  // Only ticks a box on Share the owner can see and untick; carried, never acted on here.
  const agentAsked = params.get('agent')
  const share = `${scopedPath(workspace, `/connections/${id}/share`)}?${new URLSearchParams({
    return_to: returnTo, ...(agentAsked ? { agent: agentAsked } : {}),
  })}`

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

  const copy = state ? copyFor(state) : null
  const agent = copy?.agent ?? ''
  const where = phase === 'idle' ? (waiting && justConnected.current ? 'storing' : waiting ? 'click' : 'plain') : phase
  const withExtra = (lead: string) => (copy?.extra ? `${lead} ${copy.extra}` : lead)
  // A person's reconnect: what they had lent from the earlier connection stopped with it.
  const before = state?.statement as { agents?: unknown[]; my_computers?: unknown[] } | null | undefined
  const lentBefore = (before?.agents?.length ?? 0) + (before?.my_computers?.length ?? 0)
  const reconnect = copy?.action === 'person' && state?.state === 'unshared' && lentBefore > 0
  let text = 'Connected.'
  if (reconnect && copy) text = `Connected as ${copy.account} and stored in your vault. ${lentBefore === 1 ? 'The agent or computer that used it needs' : 'The agents or computers that used it need'} it shared again.`
  else if (copy?.action === 'person') text = copy.text
  else if (copy) {
    const lead = `Connected ${agent} as ${copy.account}.`
    text = where === 'storing' ? `${lead} Storing it in ${agent}'s vault…`
      : where === 'stored' ? `Connected ${agent} as ${copy.account} and stored in ${agent}'s vault. ${agent} can use it now.${!webSealed && copy.extra ? ` ${copy.extra}` : ''}`
        : where === 'plain' ? withExtra(`${agent} can use it.`)
          : where === 'ask' ? `${lead} ${agent} needs a decision from you before it can use this.`
            : where === 'none' ? `None of ${agent}'s machines are ready yet. Try again once one is online.`
            : lead
  }
  const done = <Link to={returnTo} className={linkButton}>Done</Link>
  const machines = state && copy?.action === 'agent' ? ownerAgentMachines(state).join(', ') : ''
  const machineLine = !machines ? ''
    : where === 'stored' ? `Stored on ${agent}'s machines: ${machines}.`
      : where === 'storing' || where === 'click' ? `It goes to ${agent}'s approved machines: ${machines}.`
        : ''
  return (
    <div className="max-w-2xl space-y-4" data-testid="connected">
      <h1 className="text-lg font-semibold text-foreground">Connected</h1>
      <div className="space-y-3 rounded border border-success/30 bg-success/10 p-3 text-[13px] text-success">
        <p role="status">{text}</p>
        {machineLine && <p>{machineLine}</p>}
      </div>
      {error && <p role="alert" className="text-[13px] text-destructive">{error}</p>}
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
        {reconnect && (
          <>
            <Link to={share} className={linkButton}>Share again</Link>
            <Link to={returnTo} className={quietLink}>Later</Link>
          </>
        )}
        {!reconnect && (!copy || copy.action === 'person') && (
          <>
            {done}
            {copy && <Link to={share} className={quietLink}>Share it with agents or your computers</Link>}
          </>
        )}
        {copy?.action === 'agent' && (where === 'plain' || where === 'stored') && done}
        {copy?.action === 'agent' && where === 'click' && state && (
          <>
            <Button onClick={() => void store(state, false)}>{`Store for ${agent}`}</Button>
            <Link to={returnTo} className={quietLink}>Later</Link>
          </>
        )}
        {copy?.action === 'agent' && where === 'none' && state && (
          <>
            <Button onClick={() => void store(state, false)}>Try again</Button>
            <Link to={share} className={quietLink}>Open the Share screen</Link>
          </>
        )}
        {copy?.action === 'agent' && where === 'ask' && (
          <>
            <Link to={share} className={linkButton}>Open the Share screen</Link>
            <Link to={returnTo} className={quietLink}>Later</Link>
          </>
        )}
      </div>
      {vault.element}
    </div>
  )
}
