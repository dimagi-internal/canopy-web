import { useCallback, useEffect, useMemo, useState, type JSX } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import { Loader2, Lock, Mic, Send, Square, Trash2 } from 'lucide-react'
import { Button } from 'canopy-ui/ui'
import { listMyAgents } from '@/api/agents'
import { createSession, sendMessage } from '@/api/chat'
import { CanopyWordmark } from '@/brand/CanopyWordmark'
import { scopedPath } from '@/lib/scopedLinks'
import type { ListenDeps } from '@/listen/controller'
import type { Pairing } from '@/listen/store'
import { VoiceNote, type NoteView } from '@/listen/voiceNote'
import { Centered, defaultDeps, Meter, micSettingsHelp, Problem, SlowLaptop, useClock, useMicPermission, useWakeLock } from './ListenPage'

/** Sends the words to the agent, as this person: a new chat, the note its first message. */
export interface NoteSender {
  send(agent: { slug: string; workspace: string }, text: string): Promise<{ id: string; workspace: string }>
  agentName(agent: { slug: string; workspace: string }): Promise<string | null>
}

export const browserSender: NoteSender = {
  async send(agent, text) {
    const title = `Voice note: ${text.split(/\s+/).slice(0, 8).join(' ')}`.slice(0, 80)
    const session = await createSession({ agentSlug: agent.slug, workspace: agent.workspace, title, metadata: { source: 'voice_note' } })
    await sendMessage(session.id, text)
    return { id: session.id, workspace: session.workspace }
  },
  async agentName(agent) {
    const page = await listMyAgents({ limit: 200 })
    const found = page.items.find((a) => a.slug === agent.slug && a.workspace === agent.workspace)
    return found ? found.name || found.slug : null
  },
}

type Screen =
  | { kind: 'loading' }
  | { kind: 'unpaired'; problem?: string }
  | { kind: 'ready'; pairing: Pairing }
  | { kind: 'note'; pairing: Pairing; note: VoiceNote }

/**
 * /listen/voice?agent=<slug>&workspace=<slug> — a voice note to an agent, from the account
 * menu (Meeting assistant adds it there when its person turns it on). Record here, the paired
 * laptop reads it with its own speech model (voiceNote.ts), the words come back to edit, and
 * Send opens a chat with the agent with them as the first message. Mounted outside the app
 * shell, like /listen.
 */
export function VoiceNotePage({ deps: given, sender = browserSender }: { deps?: ListenDeps; sender?: NoteSender } = {}): JSX.Element {
  const deps = useMemo(() => given ?? defaultDeps(), [given])
  const [params] = useSearchParams()
  const agent = useMemo(
    () => ({ slug: params.get('agent') ?? '', workspace: params.get('workspace') ?? '' }),
    [params],
  )
  const [agentName, setAgentName] = useState(agent.slug)
  const [screen, setScreen] = useState<Screen>({ kind: 'loading' })

  useEffect(() => {
    let live = true
    if (agent.slug && agent.workspace) {
      sender.agentName(agent).then((name) => { if (live && name) setAgentName(name) }).catch(() => undefined)
    }
    return () => { live = false }
  }, [agent, sender])

  const goReady = useCallback(async () => {
    try {
      const pairing = await deps.store.pairing()
      setScreen(pairing ? { kind: 'ready', pairing } : { kind: 'unpaired' })
    } catch {
      setScreen({ kind: 'unpaired', problem: 'This browser won’t let Canopy keep anything on this phone (a private window does that). Open this page in a normal tab.' })
    }
  }, [deps])

  useEffect(() => { void goReady() }, [goReady])

  if (!agent.slug || !agent.workspace) {
    return (
      <Shell>
        <Centered>
          <Problem>Pick an agent from your account menu: Voice session, then the agent.</Problem>
          <Link to="/" className="text-sm underline underline-offset-4">Back to Canopy</Link>
        </Centered>
      </Shell>
    )
  }

  return (
    <Shell>
      {screen.kind === 'loading' && <Centered><Loader2 className="size-6 animate-spin text-muted-foreground" aria-label="Loading" /></Centered>}
      {screen.kind === 'unpaired' && (
        <Centered>
          {screen.problem && <Problem>{screen.problem}</Problem>}
          <p className="text-sm text-muted-foreground max-w-xs">
            Your laptop reads voice notes with its own speech model. Pair this phone with it once, in Listen.
          </p>
          <Link to="/listen" className="inline-flex h-11 items-center rounded-xl bg-primary px-5 text-sm font-medium text-primary-foreground">
            Pair with your laptop
          </Link>
        </Centered>
      )}
      {screen.kind === 'ready' && (
        <Ready
          pairing={screen.pairing}
          agentName={agentName}
          onRecord={() => {
            const note = new VoiceNote(screen.pairing, deps)
            void note.begin()
            setScreen({ kind: 'note', pairing: screen.pairing, note })
          }}
        />
      )}
      {screen.kind === 'note' && (
        <NoteScreen
          note={screen.note}
          agent={agent}
          agentName={agentName}
          sender={sender}
          onAgain={() => void goReady()}
          onRepair={async () => {
            await deps.store.setPairing(null).catch(() => undefined)
            setScreen({ kind: 'unpaired' })
          }}
        />
      )}
    </Shell>
  )
}

function Shell({ children }: { children: React.ReactNode }) {
  return (
    <div className="min-h-dvh bg-background text-foreground flex flex-col">
      <header className="flex items-center justify-between px-5 pt-[max(env(safe-area-inset-top),1rem)] pb-3">
        <Link to="/" className="text-base font-semibold">
          <CanopyWordmark />
          <span className="ml-1.5 font-normal text-muted-foreground">Voice</span>
        </Link>
        <span className="inline-flex items-center gap-1 text-xs text-muted-foreground" title="Encrypted on this device; only your laptop can open it">
          <Lock className="size-3.5" aria-hidden />
          Read on your laptop
        </span>
      </header>
      <main className="flex-1 flex flex-col w-full max-w-md mx-auto px-5 pb-[max(env(safe-area-inset-bottom),1.25rem)]">{children}</main>
    </div>
  )
}

function Ready({ pairing, agentName, onRecord }: { pairing: Pairing; agentName: string; onRecord: () => void }) {
  const mic = useMicPermission()
  return (
    <div className="flex-1 flex flex-col items-center justify-center gap-5 py-6 text-center">
      {mic === 'denied' && (
        <Problem>Microphone access is turned off for this site, and the page can’t ask again. {micSettingsHelp()}</Problem>
      )}
      <h1 className="text-lg font-semibold">Talk to {agentName}</h1>
      <button
        type="button"
        onClick={onRecord}
        aria-label="Start recording"
        className="group relative grid place-items-center size-40 rounded-full bg-destructive text-destructive-foreground shadow-lg transition active:scale-95 focus-visible:outline-none focus-visible:ring-4 focus-visible:ring-destructive/40"
      >
        <span className="absolute inset-0 rounded-full ring-8 ring-destructive/15" aria-hidden />
        <Mic className="size-14" aria-hidden />
      </button>
      <p className="text-sm text-muted-foreground max-w-xs">
        Tap and talk. {pairing.laptop_name} turns it into words, and you check them before they go to {agentName}.
      </p>
    </div>
  )
}

function useNoteView(note: VoiceNote): NoteView {
  const [view, setView] = useState(note.view)
  useEffect(() => {
    setView(note.view)
    return note.subscribe(setView)
  }, [note])
  useEffect(() => () => note.dispose(), [note])
  return view
}

function NoteScreen({
  note, agent, agentName, sender, onAgain, onRepair,
}: {
  note: VoiceNote
  agent: { slug: string; workspace: string }
  agentName: string
  sender: NoteSender
  onAgain: () => void
  onRepair: () => void
}) {
  const view = useNoteView(note)
  const navigate = useNavigate()
  const recording = view.status === 'connecting' || view.status === 'waiting' || view.status === 'recording'
  useWakeLock(recording || view.status === 'transcribing')
  const clock = useClock(view.startedAt, recording)
  const [text, setText] = useState('')
  const [sending, setSending] = useState(false)
  const [sendError, setSendError] = useState('')

  useEffect(() => {
    if (view.status === 'done') setText(view.text)
  }, [view.status, view.text])

  const send = async () => {
    const words = text.trim()
    if (!words || sending) return
    setSending(true)
    setSendError('')
    try {
      const chat = await sender.send(agent, words)
      navigate(scopedPath(chat.workspace, `/sessions/${chat.id}`))
    } catch (e) {
      setSendError(e instanceof Error && e.message ? e.message : `Couldn’t send it to ${agentName}. Try again.`)
      setSending(false)
    }
  }

  if (recording) {
    return (
      <div className="flex-1 flex flex-col items-center justify-center gap-5 py-6 text-center">
        {view.laptopSlow && <SlowLaptop name={view.laptopName} />}
        {view.reconnecting && <p role="status" className="text-sm text-muted-foreground">Reconnecting… what you say is kept on this phone.</p>}
        {view.micPaused && <p role="status" className="text-sm text-muted-foreground">Your phone paused the microphone. Tap Done to use what was heard.</p>}
        <div className="text-4xl font-semibold tabular-nums" aria-label="Recording time">{clock}</div>
        <Meter level={view.level} />
        <p className="text-sm text-muted-foreground">{view.status === 'recording' ? `Talking to ${agentName}` : `Connecting to ${view.laptopName}… keep talking`}</p>
        <div className="flex gap-3">
          <Button variant="outline" className="h-12 rounded-xl gap-2" onClick={() => note.discard()}>
            <Trash2 className="size-4" aria-hidden /> Cancel
          </Button>
          <Button className="h-12 rounded-xl gap-2 px-6" onClick={() => note.stop()}>
            <Square className="size-4" aria-hidden /> Done
          </Button>
        </div>
      </div>
    )
  }

  if (view.status === 'transcribing') {
    return (
      <Centered>
        <Loader2 className="size-6 animate-spin text-muted-foreground" aria-hidden />
        <p className="text-sm text-muted-foreground" role="status">{view.laptopName} is reading it…</p>
        {view.laptopSlow && <SlowLaptop name={view.laptopName} />}
      </Centered>
    )
  }

  if (view.status === 'done') {
    return (
      <div className="flex-1 flex flex-col gap-4 pt-2">
        <label htmlFor="note-text" className="text-sm font-medium">To {agentName}</label>
        <textarea
          id="note-text"
          value={text}
          onChange={(e) => setText(e.target.value)}
          rows={8}
          className="w-full rounded-xl border border-input bg-card px-3 py-2.5 text-base leading-relaxed focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        />
        {sendError && <Problem>{sendError}</Problem>}
        <Button className="h-12 rounded-xl gap-2 text-base" disabled={!text.trim() || sending} onClick={() => void send()}>
          {sending ? <Loader2 className="size-4 animate-spin" aria-hidden /> : <Send className="size-4" aria-hidden />}
          Send to {agentName}
        </Button>
        <Button variant="ghost" className="h-11 rounded-xl" disabled={sending} onClick={onAgain}>Record again</Button>
      </div>
    )
  }

  return (
    <Centered>
      {view.status === 'failed' && view.error && <Problem>{view.error}</Problem>}
      {view.status === 'discarded' && <p className="text-sm text-muted-foreground">Cancelled. Nothing was sent.</p>}
      {view.unpaired ? (
        <Button className="h-11 rounded-xl" onClick={onRepair}>Pair again</Button>
      ) : (
        <Button className="h-11 rounded-xl" onClick={onAgain}>Record again</Button>
      )}
    </Centered>
  )
}

export default VoiceNotePage
