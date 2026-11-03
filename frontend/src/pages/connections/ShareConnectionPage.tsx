import { useEffect, useMemo, useRef, useState, type JSX } from 'react'
import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { Button } from 'canopy-ui/ui'
import { connectionStartPath, getShareState, putShare, type ShareStateOut } from '@/api/connections'
import { ApiError } from '@/api/problem'
import {
  buildShare, changedEndorser, leftOut as leftOutOf, openForOwner, ShareRefused, trustFor, vouchersToDecide,
  type OpenedAccount, type ShareChoice, type Trust,
} from '@/vault/connectionShare'
import { pinPerson, type Endorser } from '@/vault/trust'
import type { Unlocked } from '@/vault/userKey'
import { useVaultUnlock } from '@/vault/useVaultUnlock'
import { fingerprint } from '@/vault/zk'

const FALLBACK = '/settings'

/** Where to go afterwards: a path on this site, else Settings. The query string is
 *  attacker-controllable, so anything that is not a plain absolute path is dropped. */
export function safeReturnTo(given: string | null | undefined): string {
  if (!given || !given.startsWith('/') || given.startsWith('//') || given.includes('\\')) return FALLBACK
  try {
    return new URL(given, 'http://x.invalid').origin === 'http://x.invalid' ? given : FALLBACK
  } catch {
    return FALLBACK
  }
}

const NOT_SHARED = 'Not shared yet: no agent or computer can use it.'
const RECONNECTED = 'This connection was reconnected. If you did not just do that, stop and tell an admin.'
const CHANGED = 'This connection changed in another tab or computer. Loading it again.'
const RELOCK = 'Unlock your vault key and try again.'
const NOT_YOURS = "This connection isn't yours to share."
const KEY_MOVED = 'To share it, disconnect it in Settings, then connect it again.'
const CANNOT_OPEN = "You can't open this connection's sharing right now."
const CANOPY_WEB_WHY = 'canopy-web can use this account for you, with nobody present. '
  + 'If canopy-web were ever hacked, this account could be taken until you disconnect it.'
const CANOPY_WEB_TURNS_OFF = 'Sharing with agents or your computers turns off project screens and apps for now.'
const CANOPY_WEB_NOT_YET = "This isn't available yet."
const UNSAFE = "This connection can't be opened safely. Reconnect it."

interface Opened {
  account: OpenedAccount
  /** The statement the owner last signed, verified by `openForOwner`: the only source of earlier choices. */
  statement: { agents: string[]; my_computers: string[] } | null
  trust: Trust
}

/**
 * "Connected as …" (`/w/:workspace/connections/:id/share`): where the owner lands after an
 * OAuth sign-in. Your passkey opens the envelope in this browser, which shows which
 * account was connected; the owner then chooses which agents and computers may use it
 * and shares (`vault/connectionShare.ts` signs and seals). canopy-web never sees the token.
 * Not now leaves it unshared. Settings links here to finish a share later.
 *
 * The unlocked key lives in a ref for this flow only: not in state, not rendered, not sent.
 */
export function ShareConnectionPage(): JSX.Element | null {
  const { workspace = '', id = '' } = useParams()
  const [params] = useSearchParams()
  const returnTo = safeReturnTo(params.get('return_to'))
  const navigate = useNavigate()
  const vault = useVaultUnlock()
  const me = useRef<Unlocked | null>(null)
  // Whom the owner accepted here: pinned too, but this holds while storage does not.
  const accepted = useRef<Set<string>>(new Set())

  const [state, setState] = useState<ShareStateOut | null>(null)
  const [loadError, setLoadError] = useState('')
  const [opened, setOpened] = useState<Opened | null>(null)
  const [unsafe, setUnsafe] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [dropped, setDropped] = useState<string[]>([])
  const [notYours, setNotYours] = useState(false)
  const [notOwnerKey, setNotOwnerKey] = useState('')
  const [busy, setBusy] = useState(false)
  const [agents, setAgents] = useState<Set<string>>(new Set())
  const [computers, setComputers] = useState<Set<string>>(new Set())
  const [canopyWebTicked, setCanopyWebTicked] = useState(false)

  useEffect(() => {
    let cancelled = false
    getShareState(id)
      .then((s) => !cancelled && setState(s))
      .catch((e: unknown) => {
        if (cancelled) return
        if (e instanceof ApiError && e.code === 'not_the_owner_key') setNotOwnerKey(e.detail || NOT_YOURS)
        else if (e instanceof ApiError && e.status === 404) setNotYours(true)
        else if (e instanceof ApiError && e.status === 403) setLoadError(e.detail ? `${CANNOT_OPEN} ${e.detail}` : CANNOT_OPEN)
        else setLoadError(e instanceof Error ? e.message : 'Could not read this connection.')
      })
    return () => { cancelled = true; me.current = null }
  }, [id])

  const ownerAgent = state?.owner_agent?.id ?? ''
  const machines = useMemo(
    () => (state?.candidates ?? []).filter((c) => c.kind === 'device' && !c.agent_id && c.mine),
    [state],
  )
  // The box exists only when canopy-web has a key and makes calls for this provider; it can be
  // ticked only while this deployment accepts a share that names it.
  const server = state?.server
  const canopyWebShown = !!server?.available && !!server.provider_calls
  const canopyWebEnabled = canopyWebShown && !!server?.statements_enabled
  const choice: ShareChoice = useMemo(() => ({
    agents: [...new Set([...(ownerAgent ? [ownerAgent] : []), ...agents])].sort(),
    myComputers: ownerAgent ? [] : [...computers].sort(),
    canopyWeb: canopyWebEnabled && canopyWebTicked,
  }), [agents, computers, ownerAgent, canopyWebEnabled, canopyWebTicked])
  const leftOut = useMemo(() => (state && opened ? leftOutOf(state, choice, opened.trust) : []), [state, opened, choice])
  const changed = useMemo(() => (opened ? changedEndorser(opened.trust, choice) : undefined), [opened, choice])
  const vouchers = useMemo(() => (opened ? vouchersToDecide(opened.trust, choice) : []), [opened, choice])

  if (notYours) {
    return (
      <div className="max-w-2xl space-y-2">
        <p role="alert" className="text-sm text-destructive">{NOT_YOURS}</p>
        <Link to={returnTo} className="text-sm text-primary underline">Back</Link>
      </div>
    )
  }
  if (notOwnerKey) {
    return (
      <div className="max-w-2xl space-y-2">
        <p role="alert" className="text-sm text-destructive">{notOwnerKey}</p>
        <p className="text-sm text-foreground">{KEY_MOVED}</p>
        <Link to={returnTo} className="text-sm text-primary underline">Back</Link>
      </div>
    )
  }
  if (loadError) return <p className="max-w-2xl text-sm text-destructive">{loadError}</p>
  if (!state) return <div className="h-32 max-w-2xl animate-pulse rounded-lg bg-muted" />

  const reconnect = connectionStartPath(state.provider, workspace, returnTo)

  /** Open `s` with `key` and seed the choices: `keep` (what is ticked now) or else what the
   *  verified statement last named, cut down to the agents and computers listed below. */
  async function openWith(s: ShareStateOut, key: Unlocked, keep?: ShareChoice) {
    const { account, statement } = await openForOwner(s, key)
    const trust = await trustFor(s, key, { accept: accepted.current })
    const listedAgents = new Set(s.agents.map((a) => a.id))
    const listedMachines = new Set(s.candidates.filter((c) => c.kind === 'device' && !c.agent_id && c.mine).map((c) => c.target))
    const sv = s.server
    setCanopyWebTicked(keep ? !!keep.canopyWeb : !!sv?.on)
    const wantAgents = keep ? keep.agents : statement?.agents ?? []
    const wantComputers = s.owner_agent ? [] : keep ? keep.myComputers : statement?.my_computers ?? []
    const lost = [
      ...wantAgents.filter((a) => !listedAgents.has(a)).map((a) => `agent ${a} (you no longer administer it)`),
      ...wantComputers.filter((c) => !listedMachines.has(c)).map((c) => `computer ${fingerprint(c)} (no longer one of yours)`),
    ]
    me.current = key
    setState(s)
    setAgents(new Set(wantAgents.filter((a) => listedAgents.has(a))))
    setComputers(new Set(wantComputers.filter((c) => listedMachines.has(c))))
    setDropped(lost)
    setOpened({ account, statement, trust })
  }

  async function unlock(s: ShareStateOut) {
    setBusy(true)
    setError('')
    setNotice('')
    setUnsafe(false)
    try {
      const key = await vault.unlock()
      try {
        await openWith(s, key)
      } catch (e) {
        if (e instanceof ShareRefused) setUnsafe(true)
        else throw e
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not unlock your vault key.')
    } finally {
      setBusy(false)
    }
  }

  async function share(s: ShareStateOut, o: Opened) {
    const key = me.current
    if (!key) return
    setBusy(true)
    setError('')
    setNotice('')
    try {
      const { share: body } = await buildShare(s, key, choice, o.trust)
      await putShare(s.id, body)
      me.current = null
      navigate(returnTo)
    } catch (e) {
      if (e instanceof ApiError && (e.code === 'statement_stale' || e.code === 'zk_value_changed')) {
        setNotice(CHANGED)
        try {
          await openWith(await getShareState(s.id), key, choice)
        } catch (again) {
          if (again instanceof ShareRefused) setUnsafe(true)
          else setError(again instanceof Error ? again.message : 'Could not read this connection.')
        }
      } else if (e instanceof ShareRefused && (e.code === 'no-scalar' || e.code === 'bad-owner-key')) {
        me.current = null
        setOpened(null)
        setError(RELOCK)
      } else if (e instanceof ShareRefused && e.code !== 'key-changed' && e.code !== 'too-many'
                 && e.code !== 'server-key' && e.code !== 'server-unavailable') setUnsafe(true)
      else setError(e instanceof Error ? e.message : 'Could not share this connection.')
    } finally {
      setBusy(false)
    }
  }

  /** The owner read this person's name and fingerprint and accepted them: pin them, as
   *  everywhere else, and read again. `buildShare` then names them in `endorsers`. */
  async function trustVoucher(s: ShareStateOut, o: Opened, voucher: Endorser) {
    const key = me.current
    if (!key) return
    setBusy(true)
    setError('')
    try {
      pinPerson(voucher.by, voucher.jkt, voucher.email)
      accepted.current.add(voucher.by)
      setOpened({ ...o, trust: await trustFor(s, key, { accept: accepted.current }) })
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not trust them.')
    } finally {
      setBusy(false)
    }
  }

  const toggle = (set: Set<string>, setter: (s: Set<string>) => void, key: string) => {
    const next = new Set(set)
    if (!next.delete(key)) next.add(key)
    setter(next)
  }
  // A first share has to reach someone. Once shared, unticking everyone is how the last
  // grant is withdrawn: a statement naming nobody, signed like any other.
  const nothingToShare = !opened?.statement && choice.agents.length === 0 && choice.myComputers.length === 0
    && !choice.canopyWeb
  const reconnected = !!opened?.statement && state.sealed_sig == null

  return (
    <div className="max-w-2xl space-y-4" data-testid="share-connection">
      <h1 className="text-lg font-semibold text-foreground">Share this connection</h1>

      {unsafe ? (
        <div role="alert" className="space-y-2 rounded border border-destructive/30 bg-destructive/10 p-3 text-[13px] text-destructive">
          <p>{UNSAFE}</p>
          <a href={reconnect} className="inline-flex rounded bg-primary px-3 py-1.5 text-[13px] font-medium text-primary-foreground hover:bg-primary/90">
            Reconnect
          </a>
        </div>
      ) : !opened ? (
        <div className="space-y-3">
          <p className="text-sm text-muted-foreground">
            Touch your passkey to open it here. canopy never sees the sign-in it holds.
          </p>
          {state.state === 'unshared' && <p className="text-[13px] text-muted-foreground">{NOT_SHARED}</p>}
          <Button onClick={() => void unlock(state)} disabled={busy}>Unlock with your passkey</Button>
          {error && <p role="alert" className="text-[13px] text-destructive">{error}</p>}
          {vault.element}
        </div>
      ) : (
        <div className="space-y-4">
          <div>
            {state.account_label ? (
              <>
                <p className="text-sm font-medium text-foreground">Connected as {state.account_label}</p>
                <p className="text-[12px] text-muted-foreground">Account ID: {opened.account.account_id}</p>
              </>
            ) : (
              <p className="text-sm font-medium text-foreground">Connected as {opened.account.account_id}</p>
            )}
            {opened.account.instance_url && (
              <p className="text-[12px] text-muted-foreground">{opened.account.instance_url}</p>
            )}
          </div>
          {reconnected && (
            <p role="alert" className="rounded border border-warning/40 bg-warning/10 p-3 text-[13px] text-foreground">{RECONNECTED}</p>
          )}
          <p className="text-sm text-muted-foreground">Choose who may use it.</p>

          <fieldset className="space-y-2">
            <legend className="text-sm font-semibold text-foreground">Agents</legend>
            {state.agents.length === 0 && <p className="text-[13px] text-muted-foreground">You administer no agents here.</p>}
            {state.agents.map((a) => {
              const fixed = a.id === ownerAgent
              return (
                <label key={a.id} className="flex items-center gap-2 text-sm">
                  <input
                    type="checkbox"
                    checked={fixed || agents.has(a.id)}
                    disabled={fixed || busy}
                    onChange={() => toggle(agents, setAgents, a.id)}
                  />
                  <span>{a.name}</span>
                </label>
              )
            })}
          </fieldset>

          {!ownerAgent && (
            <fieldset className="space-y-2">
              <legend className="text-sm font-semibold text-foreground">Use on my computers</legend>
              {machines.length === 0 && (
                <p className="text-[13px] text-muted-foreground">No enrolled computers. Run: canopy vault device --enroll</p>
              )}
              {machines.map((m) => (
                <div key={m.target} className="flex items-start gap-2 text-sm">
                  <input
                    id={`m-${m.target}`}
                    type="checkbox"
                    aria-label={m.runner_name || 'Unnamed computer'}
                    checked={computers.has(m.target)}
                    disabled={busy}
                    onChange={() => toggle(computers, setComputers, m.target)}
                  />
                  <label htmlFor={`m-${m.target}`} className="space-y-0.5">
                    <span className="block">{m.runner_name || 'Unnamed computer'}</span>
                    <span className="block font-mono text-[12px] text-muted-foreground">{fingerprint(m.target)}</span>
                  </label>
                </div>
              ))}
              {machines.length > 0 && (
                <p className="text-[12px] text-muted-foreground">
                  Check the fingerprint against the one on that computer: it is the only way to tell it from one canopy invented.
                </p>
              )}
            </fieldset>
          )}

          {canopyWebShown && (
            <fieldset className="space-y-1">
              <legend className="text-sm font-semibold text-foreground">Project screens and apps</legend>
              <div className="flex items-start gap-2 text-sm">
                <input
                  id="canopy-web"
                  type="checkbox"
                  aria-label="Project screens and apps"
                  aria-describedby="canopy-web-why"
                  checked={canopyWebEnabled && canopyWebTicked}
                  disabled={busy || !canopyWebEnabled}
                  onChange={() => setCanopyWebTicked(!canopyWebTicked)}
                />
                <label htmlFor="canopy-web" id="canopy-web-why" className="space-y-0.5 text-[13px] text-muted-foreground">
                  {CANOPY_WEB_WHY}
                </label>
              </div>
              {!canopyWebEnabled && (
                <p data-testid="canopy-web-unavailable" className="text-[13px] text-muted-foreground">
                  {server?.on ? CANOPY_WEB_TURNS_OFF : CANOPY_WEB_NOT_YET}
                </p>
              )}
            </fieldset>
          )}

          {leftOut.length > 0 && (
            <ul data-testid="left-out" className="space-y-1 rounded border border-warning/40 bg-warning/10 p-3 text-[13px] text-foreground">
              {leftOut.map((d) => (
                <li key={d.target}>{d.runner_name || 'A computer'} will be left out: {d.why}.</li>
              ))}
            </ul>
          )}
          {vouchers.length > 0 && !changed && (
            <div data-testid="vouchers" className="space-y-2 rounded border border-warning/40 bg-warning/10 p-3 text-[13px] text-foreground">
              {vouchers.map((v) => (
                <div key={v.by} className="space-y-1">
                  <p>
                    {v.email || 'Someone'} vouches for machines you have chosen, and this browser has not trusted
                    them yet. Check this fingerprint with them first:
                  </p>
                  <p className="font-mono text-[12px] text-muted-foreground">{fingerprint(v.jkt)}</p>
                  <Button size="sm" variant="outline" disabled={busy} onClick={() => void trustVoucher(state, opened, v)}>
                    Trust {v.email || 'this person'}’s vouching
                  </Button>
                </div>
              ))}
            </div>
          )}
          {changed && (
            <p role="alert" className="rounded border border-destructive/30 bg-destructive/10 p-3 text-[13px] text-destructive">
              {changed.email || 'Someone'}’s vault key changed since this browser last trusted it. Confirm with
              them before sharing: nothing was sent.
            </p>
          )}
          {notice && <p role="status" className="text-[13px] text-foreground">{notice}</p>}
          {dropped.length > 0 && (
            <p data-testid="dropped" className="text-[13px] text-foreground">
              Not carried over from before: {dropped.join('; ')}.
            </p>
          )}
          {error && <p role="alert" className="text-[13px] text-destructive">{error}</p>}

          <p className="text-[13px] text-muted-foreground">{state.state === 'unshared' ? NOT_SHARED : null}</p>
          <div className="flex gap-2">
            <Button onClick={() => void share(state, opened)} disabled={busy || nothingToShare || !!changed}>Share</Button>
            <Button variant="outline" onClick={() => navigate(returnTo)} disabled={busy}>Not now</Button>
          </div>
        </div>
      )}
    </div>
  )
}
