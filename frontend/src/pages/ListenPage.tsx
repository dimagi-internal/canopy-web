import { useCallback, useEffect, useMemo, useRef, useState, type JSX, type ReactNode } from 'react'
import {
  Check,
  CircleCheck,
  ExternalLink,
  Laptop,
  Loader2,
  Lock,
  MessageSquare,
  Mic,
  QrCode,
  Smartphone,
  Square,
  Trash2,
  TriangleAlert,
  WifiOff,
} from 'lucide-react'
import { personColor } from 'canopy-ui/chat'
import { Button, Dialog, DialogContent, DialogHeader, DialogTitle } from 'canopy-ui/ui'
import { endListenSession, listListenRunners, openListenSession } from '@/api/listen'
import { CanopyWordmark } from '@/brand/CanopyWordmark'
import { wsUrl } from '@/lib/wsUrl'
import { MicDenied, startMic } from '@/listen/audio'
import { Meeting, pairPhone, type ListenDeps, type MeetingView, type PairStep } from '@/listen/controller'
import { captureListenPairing, isExpired, parsePairFragment, takeStashedPairing, type PairInvite } from '@/listen/pairLink'
import { browserSocket } from '@/listen/relay'
import { browserStore, deviceLabel, type ListenStore, type Pairing } from '@/listen/store'

/** The laptop's own page, where a phone is paired. */
export const LAPTOP_PAGE = 'http://127.0.0.1:7351'
export const NOTICE = "What's said here leaves this phone for your laptop and your agent. Tell the people here."

type Screen =
  | { kind: 'loading' }
  | { kind: 'unpaired'; problem?: string }
  | { kind: 'pairing'; invite: PairInvite; step: PairStep; error?: string }
  | { kind: 'home'; pairing: Pairing; noticeAccepted: boolean }
  | { kind: 'meeting'; pairing: Pairing; meeting: Meeting }

export function defaultDeps(store: ListenStore = browserStore): ListenDeps {
  return {
    openSession: openListenSession,
    endSession: endListenSession,
    listRunners: listListenRunners,
    store,
    socket: browserSocket,
    wsUrl,
    label: deviceLabel(),
    startMic: async (onChunk, onLevel) => {
      try {
        return await startMic(onChunk, onLevel)
      } catch (e) {
        if (e instanceof MicDenied && e.why === 'denied') {
          throw new Error(
            'Microphone access is off for this site. Allow it in your browser’s site settings (on iPhone: aA → Website Settings → Microphone), then try again.',
          )
        }
        throw e
      }
    },
    setTimeout: (fn, ms) => window.setTimeout(fn, ms),
    clearTimeout: (h) => window.clearTimeout(h as number),
  }
}

/**
 * /listen — record a meeting on this phone; the owner's laptop transcribes it.
 *
 * Audio goes phone → canopy-web → the laptop's runner → canopy-listen, end-to-end
 * encrypted (relayCrypto.ts): Canopy carries ciphertext only. Pairing happens once, from
 * the QR code on the laptop's own page; the link's `#pair=` fragment is taken out of the
 * address before anything else runs (pairLink.ts). Mounted outside the app shell: it is
 * a phone screen, not a page of the app. Design: docs/architecture/listen-relay.md.
 */
export function ListenPage({ deps: given }: { deps?: ListenDeps } = {}): JSX.Element {
  const deps = useMemo(() => given ?? defaultDeps(), [given])
  const [screen, setScreen] = useState<Screen>({ kind: 'loading' })

  const goHome = useCallback(async () => {
    try {
      const pairing = await deps.store.pairing()
      if (!pairing) {
        setScreen({ kind: 'unpaired' })
        return
      }
      setScreen({ kind: 'home', pairing, noticeAccepted: await deps.store.noticeAccepted() })
    } catch {
      setScreen({ kind: 'unpaired', problem: 'This browser won’t let Canopy keep anything on this phone (a private window does that). Open this page in a normal tab.' })
    }
  }, [deps])

  const startPairing = useCallback(
    (invite: PairInvite) => {
      if (isExpired(invite)) {
        setScreen({ kind: 'unpaired', problem: 'That pairing code has expired. Make a new one on your laptop and scan it again.' })
        return
      }
      setScreen({ kind: 'pairing', invite, step: 'connecting' })
      pairPhone(invite, deps, (step) => setScreen((s) => (s.kind === 'pairing' ? { ...s, step } : s)))
        .then(() => window.setTimeout(() => void goHome(), 900))
        .catch((e: unknown) =>
          setScreen((s) => (s.kind === 'pairing' ? { ...s, error: e instanceof Error ? e.message : 'Pairing failed.' } : s)),
        )
    },
    [deps, goHome],
  )

  useEffect(() => {
    const readInvite = () => {
      captureListenPairing() // a same-tab scan only changes the hash: no reload, so catch it here too
      const stashed = takeStashedPairing()
      if (!stashed) return false
      const invite = parsePairFragment(stashed)
      if (!invite) {
        setScreen({ kind: 'unpaired', problem: 'That pairing link is incomplete. Scan the QR code on your laptop again.' })
        return true
      }
      startPairing(invite)
      return true
    }
    if (!readInvite()) void goHome()
    const onHash = () => void readInvite()
    window.addEventListener('hashchange', onHash)
    return () => window.removeEventListener('hashchange', onHash)
  }, [goHome, startPairing])

  return (
    <div className="min-h-dvh bg-background text-foreground flex flex-col">
      <header className="flex items-center justify-between px-5 pt-[max(env(safe-area-inset-top),1rem)] pb-3">
        <span className="text-base font-semibold">
          <CanopyWordmark />
          <span className="ml-1.5 font-normal text-muted-foreground">Listen</span>
        </span>
        <span className="inline-flex items-center gap-1 text-xs text-muted-foreground" title="Encrypted on this phone; only your laptop can open it">
          <Lock className="size-3.5" aria-hidden />
          End-to-end encrypted
        </span>
      </header>
      <main className="flex-1 flex flex-col w-full max-w-md mx-auto px-5 pb-[max(env(safe-area-inset-bottom),1.25rem)]">
        {screen.kind === 'loading' && <Centered><Loader2 className="size-6 animate-spin text-muted-foreground" aria-label="Loading" /></Centered>}
        {screen.kind === 'unpaired' && <Unpaired problem={screen.problem} />}
        {screen.kind === 'pairing' && <PairingScreen screen={screen} onDone={() => void goHome()} />}
        {screen.kind === 'home' && (
          <Home
            deps={deps}
            pairing={screen.pairing}
            noticeAccepted={screen.noticeAccepted}
            onAccept={async () => {
              await deps.store.acceptNotice().catch(() => undefined)
              setScreen({ ...screen, noticeAccepted: true })
            }}
            onUnpair={async () => {
              await deps.store.setPairing(null).catch(() => undefined)
              setScreen({ kind: 'unpaired' })
            }}
            onRecord={() => {
              const meeting = new Meeting(screen.pairing, deps)
              void meeting.begin()
              setScreen({ kind: 'meeting', pairing: screen.pairing, meeting })
            }}
          />
        )}
        {screen.kind === 'meeting' && <MeetingScreen meeting={screen.meeting} onBack={() => void goHome()} />}
      </main>
    </div>
  )
}

function Centered({ children }: { children: ReactNode }) {
  return <div className="flex-1 flex flex-col items-center justify-center text-center gap-4">{children}</div>
}

function Problem({ children }: { children: ReactNode }) {
  return (
    <div role="alert" className="flex gap-2.5 rounded-xl border border-destructive/30 bg-destructive/10 px-4 py-3 text-sm text-destructive text-left">
      <TriangleAlert className="size-4 mt-0.5 shrink-0" aria-hidden />
      <span>{children}</span>
    </div>
  )
}

// -- (a) not paired -------------------------------------------------------------------

function Unpaired({ problem }: { problem?: string }) {
  return (
    <div className="flex-1 flex flex-col justify-center gap-7 py-6">
      <div className="flex items-center justify-center gap-3 text-primary" aria-hidden>
        <span className="grid place-items-center size-14 rounded-2xl bg-primary/10"><Smartphone className="size-7" /></span>
        <span className="h-px w-8 bg-border" />
        <span className="grid place-items-center size-14 rounded-2xl bg-primary/10"><Laptop className="size-7" /></span>
      </div>
      <div className="text-center space-y-2">
        <h1 className="text-2xl font-semibold tracking-tight">Record meetings from your phone</h1>
        <p className="text-sm text-muted-foreground leading-relaxed">
          This phone records; your laptop transcribes and your agent takes notes. The audio is encrypted here and only
          your laptop can open it — Canopy just carries it.
        </p>
      </div>
      {problem && <Problem>{problem}</Problem>}
      <ol className="rounded-2xl border border-border bg-card divide-y divide-border">
        {[
          <>On your laptop, open <span className="font-medium text-foreground">Canopy Listen</span> at{' '}
            <span className="font-mono text-xs text-foreground break-all">{LAPTOP_PAGE}</span></>,
          <>Choose <span className="font-medium text-foreground">“Pair a phone”</span></>,
          <>Scan the QR code with this phone’s camera</>,
        ].map((step, i) => (
          <li key={i} className="flex items-start gap-3 px-4 py-3.5 text-sm text-muted-foreground">
            <span className="grid place-items-center size-6 shrink-0 rounded-full bg-primary text-primary-foreground text-xs font-semibold">{i + 1}</span>
            <span className="pt-0.5">{step}</span>
          </li>
        ))}
      </ol>
      <p className="flex items-center justify-center gap-1.5 text-xs text-muted-foreground">
        <QrCode className="size-3.5" aria-hidden /> Pairing happens once per phone.
      </p>
    </div>
  )
}

// -- (b) pairing ----------------------------------------------------------------------

function PairingScreen({ screen, onDone }: { screen: Extract<Screen, { kind: 'pairing' }>; onDone: () => void }) {
  const name = screen.invite.laptopName
  const steps: Array<[PairStep, string]> = [
    ['connecting', `Reaching ${name}`],
    ['waiting', `Waiting for ${name} to confirm`],
    ['done', 'Paired'],
  ]
  const at = steps.findIndex(([s]) => s === screen.step)
  return (
    <Centered>
      <span className="grid place-items-center size-16 rounded-2xl bg-primary/10 text-primary">
        {screen.step === 'done' ? <Check className="size-8" aria-hidden /> : <Laptop className="size-8" aria-hidden />}
      </span>
      <h1 className="text-xl font-semibold">Pairing with {name}</h1>
      <ul className="w-full space-y-2 text-left">
        {steps.map(([step, label], i) => (
          <li key={step} className="flex items-center gap-3 rounded-xl border border-border bg-card px-4 py-3 text-sm">
            {i < at || screen.step === 'done' ? (
              <CircleCheck className="size-5 text-success" aria-hidden />
            ) : i === at && !screen.error ? (
              <Loader2 className="size-5 animate-spin text-primary" aria-hidden />
            ) : (
              <span className="size-5 rounded-full border-2 border-border" aria-hidden />
            )}
            <span className={i <= at ? 'text-foreground' : 'text-muted-foreground'}>{label}</span>
          </li>
        ))}
      </ul>
      {screen.error && (
        <div className="w-full space-y-3">
          <Problem>{screen.error}</Problem>
          <Button variant="outline" className="w-full h-12 rounded-xl" onClick={onDone}>Back</Button>
        </div>
      )}
    </Centered>
  )
}

// -- (c) paired: home ------------------------------------------------------------------

function useRunnerOnline(deps: ListenDeps, runnerId: string): { online: boolean | null; gone: boolean } {
  const [state, setState] = useState<{ online: boolean | null; gone: boolean }>({ online: null, gone: false })
  useEffect(() => {
    let alive = true
    const poll = async () => {
      try {
        const runners = await deps.listRunners()
        const mine = runners.find((r) => r.id === runnerId)
        if (alive) setState({ online: mine ? mine.online : false, gone: !mine })
      } catch {
        if (alive) setState((s) => ({ ...s, online: s.online ?? false }))
      }
    }
    void poll()
    const timer = window.setInterval(() => void poll(), 15_000)
    const onVisible = () => document.visibilityState === 'visible' && void poll()
    document.addEventListener('visibilitychange', onVisible)
    return () => {
      alive = false
      window.clearInterval(timer)
      document.removeEventListener('visibilitychange', onVisible)
    }
  }, [deps, runnerId])
  return state
}

function Home({
  deps, pairing, noticeAccepted, onAccept, onUnpair, onRecord,
}: {
  deps: ListenDeps
  pairing: Pairing
  noticeAccepted: boolean
  onAccept: () => void
  onUnpair: () => void
  onRecord: () => void
}) {
  const { online, gone } = useRunnerOnline(deps, pairing.runner_id)
  const [confirmUnpair, setConfirmUnpair] = useState(false)
  const canRecord = online === true && noticeAccepted
  return (
    <div className="flex-1 flex flex-col gap-5 pt-2">
      <section className="flex items-center gap-3 rounded-2xl border border-border bg-card px-4 py-3.5">
        <span className="grid place-items-center size-10 rounded-xl bg-primary/10 text-primary"><Laptop className="size-5" aria-hidden /></span>
        <div className="min-w-0 flex-1">
          <div className="truncate font-medium">{pairing.laptop_name}</div>
          <div className="flex items-center gap-1.5 text-xs text-muted-foreground" aria-live="polite">
            <span
              className={`size-2 rounded-full ${online ? 'bg-success' : online === false ? 'bg-muted-foreground/50' : 'bg-border animate-pulse'}`}
              aria-hidden
            />
            {online === null ? 'Checking…' : online ? 'Online' : 'Offline'}
          </div>
        </div>
      </section>

      {online === false && (
        <p className="flex gap-2 rounded-xl bg-muted px-4 py-3 text-sm text-muted-foreground">
          <WifiOff className="size-4 mt-0.5 shrink-0" aria-hidden />
          {gone
            ? 'This laptop is no longer one of your computers on Canopy. Pair again from the laptop you want to use.'
            : `Open ${pairing.laptop_name} and make sure Canopy Runner is running. This updates by itself.`}
        </p>
      )}

      {!noticeAccepted && (
        <section className="rounded-2xl border border-warning/30 bg-warning/10 px-4 py-4 space-y-3">
          <p className="text-sm font-medium text-foreground leading-relaxed">{NOTICE}</p>
          <Button className="w-full h-12 rounded-xl text-base" onClick={onAccept}>I’ll tell them</Button>
        </section>
      )}

      <div className="flex-1 flex flex-col items-center justify-center gap-4 py-6">
        <button
          type="button"
          onClick={onRecord}
          disabled={!canRecord}
          aria-label="Start recording"
          className="group relative grid place-items-center size-40 rounded-full bg-destructive text-destructive-foreground shadow-lg transition active:scale-95 disabled:opacity-35 disabled:shadow-none focus-visible:outline-none focus-visible:ring-4 focus-visible:ring-destructive/40"
        >
          <span className="absolute inset-0 rounded-full ring-8 ring-destructive/15 group-disabled:ring-0" aria-hidden />
          <Mic className="size-14" aria-hidden />
        </button>
        <p className="text-sm text-muted-foreground text-center min-h-5">
          {!noticeAccepted ? 'Acknowledge the notice to start' : online ? 'Tap to start recording' : online === false ? 'Waiting for your laptop' : ''}
        </p>
        {noticeAccepted && <p className="text-xs text-muted-foreground text-center max-w-xs">{NOTICE}</p>}
      </div>

      <div className="text-center">
        {confirmUnpair ? (
          <div className="flex items-center justify-center gap-2 text-sm">
            <span className="text-muted-foreground">Forget {pairing.laptop_name}?</span>
            <Button variant="destructive" size="sm" onClick={onUnpair}>Unpair</Button>
            <Button variant="ghost" size="sm" onClick={() => setConfirmUnpair(false)}>Keep</Button>
          </div>
        ) : (
          <button type="button" className="text-xs text-muted-foreground underline underline-offset-4 py-2" onClick={() => setConfirmUnpair(true)}>
            Unpair this phone
          </button>
        )}
      </div>
    </div>
  )
}

// -- (d) recording and (e) ended -------------------------------------------------------

function useMeetingView(meeting: Meeting): MeetingView {
  const [view, setView] = useState(meeting.view)
  useEffect(() => {
    setView(meeting.view)
    return meeting.subscribe(setView)
  }, [meeting])
  useEffect(() => () => meeting.dispose(), [meeting])
  return view
}

/** Keeps the screen on while `active`; re-asked when the page is shown again. */
function useWakeLock(active: boolean) {
  useEffect(() => {
    if (!active || !('wakeLock' in navigator)) return
    let lock: { release(): Promise<void> } | null = null
    let cancelled = false
    const take = async () => {
      try {
        const got = await (navigator as Navigator & { wakeLock: { request(t: 'screen'): Promise<{ release(): Promise<void> }> } }).wakeLock.request('screen')
        if (cancelled) void got.release()
        else lock = got
      } catch {
        // Refused (battery saver): recording still works, the screen may just sleep.
      }
    }
    const onVisible = () => document.visibilityState === 'visible' && void take()
    void take()
    document.addEventListener('visibilitychange', onVisible)
    return () => {
      cancelled = true
      document.removeEventListener('visibilitychange', onVisible)
      void lock?.release().catch(() => undefined)
    }
  }, [active])
}

function useClock(since: number | undefined, running: boolean): string {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    if (!running) return
    const t = window.setInterval(() => setNow(Date.now()), 500)
    return () => window.clearInterval(t)
  }, [running])
  if (!since) return '0:00'
  const s = Math.max(0, Math.floor((now - since) / 1000))
  const h = Math.floor(s / 3600)
  const mm = Math.floor((s % 3600) / 60)
  const ss = String(s % 60).padStart(2, '0')
  return h ? `${h}:${String(mm).padStart(2, '0')}:${ss}` : `${mm}:${ss}`
}

function Meter({ level }: { level: number }) {
  const bars = 24
  return (
    <div className="flex items-end justify-center gap-[3px] h-8" aria-hidden>
      {Array.from({ length: bars }, (_, i) => {
        const centre = 1 - Math.abs(i - (bars - 1) / 2) / (bars / 2)
        const h = Math.max(0.12, Math.min(1, level * (0.55 + centre * 0.9)))
        return <span key={i} className="w-1 rounded-full bg-destructive/80 transition-[height] duration-100" style={{ height: `${h * 100}%` }} />
      })}
    </div>
  )
}

function speakerName(view: MeetingView, n: number): string {
  return view.speakers[n] || `Speaker ${n + 1}`
}

function MeetingScreen({ meeting, onBack }: { meeting: Meeting; onBack: () => void }) {
  const view = useMeetingView(meeting)
  const live = view.status === 'recording' || view.status === 'waiting' || view.status === 'connecting'
  useWakeLock(live || view.status === 'ending')
  const clock = useClock(view.startedAt, view.status === 'recording')
  const [renaming, setRenaming] = useState<number | null>(null)
  const [confirmDiscard, setConfirmDiscard] = useState(false)
  const listRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    const el = listRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [view.segments.length])

  if (view.status === 'saved' || view.status === 'discarded' || view.status === 'failed') {
    return <Ended view={view} onBack={onBack} />
  }

  return (
    <div className="flex-1 flex flex-col min-h-0 gap-4">
      <section className="rounded-2xl border border-border bg-card px-4 pt-4 pb-3 space-y-3">
        <div className="flex items-center justify-between">
          <span className="inline-flex items-center gap-2 text-sm font-medium">
            {view.status === 'recording' ? (
              <><span className="size-2.5 rounded-full bg-destructive animate-pulse" aria-hidden />Recording</>
            ) : view.status === 'ending' ? (
              <><Loader2 className="size-4 animate-spin" aria-hidden />Saving…</>
            ) : (
              <><Loader2 className="size-4 animate-spin text-primary" aria-hidden />Waiting for {view.laptopName}…</>
            )}
          </span>
          <span className="font-mono text-2xl tabular-nums tracking-tight" aria-label="Elapsed time">{clock}</span>
        </div>
        <Meter level={view.status === 'ending' ? 0 : view.level} />
        <div className="flex items-center gap-1.5 text-xs text-muted-foreground">
          <Laptop className="size-3.5" aria-hidden />
          <span className="truncate">{view.laptopName}</span>
          <span aria-hidden>·</span>
          <span>{view.reconnecting ? 'Reconnecting…' : view.laptopConnected ? 'connected' : 'not connected yet'}</span>
        </div>
      </section>

      {view.chatUrl && (
        <a
          href={view.chatUrl}
          target="_blank"
          rel="noreferrer"
          className="flex items-center gap-3 rounded-xl border border-primary/30 bg-primary/10 px-4 py-3 text-sm font-medium text-primary"
        >
          <MessageSquare className="size-4" aria-hidden />
          <span className="flex-1">Open the agent chat</span>
          <ExternalLink className="size-4" aria-hidden />
        </a>
      )}

      <div ref={listRef} className="flex-1 min-h-40 overflow-y-auto overscroll-contain rounded-2xl bg-muted/40 px-4 py-3 space-y-3" aria-live="polite">
        {view.segments.length === 0 ? (
          <p className="h-full grid place-items-center text-sm text-muted-foreground text-center py-10">
            {view.status === 'recording' ? 'Listening… the transcript appears here.' : 'The transcript appears here once your laptop is listening.'}
          </p>
        ) : (
          view.segments.map((seg, i) => {
            const color = personColor(seg.speaker)
            const sameAsBefore = i > 0 && view.segments[i - 1].speaker === seg.speaker
            return (
              <div key={i} className="text-[15px] leading-relaxed">
                {!sameAsBefore && (
                  <button
                    type="button"
                    className={`text-xs font-semibold ${color.text} py-1 pr-2`}
                    onClick={() => setRenaming(seg.speaker)}
                    aria-label={`Rename ${speakerName(view, seg.speaker)}`}
                  >
                    {speakerName(view, seg.speaker)}
                  </button>
                )}
                <p className="text-foreground">{seg.text}</p>
              </div>
            )
          })
        )}
      </div>

      <div className="grid grid-cols-[auto_1fr] gap-3 pt-1">
        {confirmDiscard ? (
          <div className="col-span-2 flex flex-col gap-2 rounded-2xl border border-destructive/30 bg-destructive/10 p-3">
            <p className="text-sm text-foreground">Discard this recording? Your laptop deletes what it has.</p>
            <div className="grid grid-cols-2 gap-2">
              <Button variant="destructive" className="h-12 rounded-xl" onClick={() => meeting.discard()}>Discard</Button>
              <Button variant="outline" className="h-12 rounded-xl" onClick={() => setConfirmDiscard(false)}>Keep recording</Button>
            </div>
          </div>
        ) : (
          <>
            <Button variant="outline" className="h-14 rounded-xl px-5" disabled={view.status === 'ending'} onClick={() => setConfirmDiscard(true)} aria-label="Discard">
              <Trash2 className="size-5" aria-hidden />
            </Button>
            <Button className="h-14 rounded-xl text-base gap-2" disabled={view.status === 'ending'} onClick={() => meeting.end()}>
              <Square className="size-4 fill-current" aria-hidden />
              End meeting
            </Button>
          </>
        )}
      </div>

      {renaming !== null && (
        <RenameSheet
          current={speakerName(view, renaming)}
          onCancel={() => setRenaming(null)}
          onSave={(name) => {
            meeting.rename(renaming, name)
            setRenaming(null)
          }}
        />
      )}
    </div>
  )
}

function RenameSheet({ current, onCancel, onSave }: { current: string; onCancel: () => void; onSave: (name: string) => void }) {
  const [name, setName] = useState(current)
  return (
    <Dialog open onOpenChange={(open) => !open && onCancel()}>
      <DialogContent className="top-auto bottom-0 translate-y-0 max-w-md rounded-b-none rounded-t-3xl px-5 pt-6 pb-[max(env(safe-area-inset-bottom),1.25rem)]">
        <DialogHeader>
          <DialogTitle>Who is “{current}”?</DialogTitle>
        </DialogHeader>
        <form
          className="space-y-4"
          onSubmit={(e) => {
            e.preventDefault()
            onSave(name)
          }}
        >
          <input
            autoFocus
            aria-label="Speaker name"
            value={name}
            onChange={(e) => setName(e.target.value)}
            maxLength={80}
            className="w-full h-12 rounded-xl border border-input bg-background px-3 text-base focus:outline-none focus:ring-2 focus:ring-ring"
          />
          <div className="grid grid-cols-2 gap-2">
            <Button type="button" variant="outline" className="h-12 rounded-xl" onClick={onCancel}>Cancel</Button>
            <Button type="submit" className="h-12 rounded-xl" disabled={!name.trim()}>Rename</Button>
          </div>
        </form>
      </DialogContent>
    </Dialog>
  )
}

function Ended({ view, onBack }: { view: MeetingView; onBack: () => void }) {
  return (
    <Centered>
      {view.status === 'saved' && (
        <>
          <span className="grid place-items-center size-16 rounded-full bg-success/15 text-success"><Check className="size-8" aria-hidden /></span>
          <h1 className="text-xl font-semibold">Saved on {view.laptopName}</h1>
          <p className="text-sm text-muted-foreground">Your agent has the transcript.</p>
          {view.savedUrl && (
            <a href={view.savedUrl} target="_blank" rel="noreferrer" className="inline-flex items-center gap-2 text-sm font-medium text-primary underline underline-offset-4">
              Open the meeting <ExternalLink className="size-4" aria-hidden />
            </a>
          )}
          {view.chatUrl && (
            <a href={view.chatUrl} target="_blank" rel="noreferrer" className="inline-flex items-center gap-2 text-sm font-medium text-primary underline underline-offset-4">
              Open the agent chat <ExternalLink className="size-4" aria-hidden />
            </a>
          )}
        </>
      )}
      {view.status === 'discarded' && (
        <>
          <span className="grid place-items-center size-16 rounded-full bg-muted text-muted-foreground"><Trash2 className="size-7" aria-hidden /></span>
          <h1 className="text-xl font-semibold">Discarded</h1>
          <p className="text-sm text-muted-foreground">Nothing from this recording is kept.</p>
        </>
      )}
      {view.status === 'failed' && (
        <>
          <span className="grid place-items-center size-16 rounded-full bg-destructive/10 text-destructive"><TriangleAlert className="size-7" aria-hidden /></span>
          <h1 className="text-xl font-semibold">The recording stopped</h1>
          <Problem>{view.error ?? 'Something went wrong.'}</Problem>
          {view.segments.length > 0 && (
            <p className="text-xs text-muted-foreground">What was sent before this is on {view.laptopName}.</p>
          )}
        </>
      )}
      <Button className="w-full h-12 rounded-xl text-base mt-4" onClick={onBack}>
        {view.status === 'failed' ? 'Back' : 'Record another'}
      </Button>
    </Centered>
  )
}

export default ListenPage
