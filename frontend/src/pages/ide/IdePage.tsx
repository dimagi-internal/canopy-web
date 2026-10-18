import { useCallback, useEffect, useMemo, useRef, useState, type KeyboardEvent, type PointerEvent as ReactPointerEvent } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { clsx } from 'clsx'
import { ChevronDown, Menu, MoreHorizontal } from 'lucide-react'
import { WorkbenchRail, WorkbenchShell } from 'canopy-ui'
import {
  DropdownMenu,
  DropdownMenuCheckboxItem,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuShortcut,
  DropdownMenuTrigger,
} from 'canopy-ui/ui'
import { setAppBarHidden, useAppBarHidden } from '@/components/AppLayout/appBarStore'
import {
  archiveSession,
  closeSession,
  listSessions,
  stopSession,
  transferSession,
  unarchiveSession,
  type ChatSession,
} from '@/api/chat'
import { listRunners, type RunnerOut } from '@/api/harness'
import { listAgents, type AgentOut } from '@/api/agents'
import { NewChatMenu } from '@/components/chat/NewChatMenu'
import { wsUrl } from '@/lib/wsUrl'
import { ChatPage } from '@/pages/ChatPage'
import { sessionDisplayTitle } from '@/components/chat/sessionDisplayTitle'
import { closeResultMessage, settleClosing } from '@/components/chat/closeAction'
import { SessionContextMenu, type IdeAction } from './SessionContextMenu'
import { useCloseReview } from './useCloseReview'
import {
  DEFAULT_FILTERS,
  NO_RUNNER,
  buildTree,
  deleteConsequence,
  runnerKey,
  sessionMode,
  sessionState,
  shortAge,
  type IdeFilters,
  type IdeMode,
  type IdeState,
} from './ideTree'

/**
 * /ide — a desktop, emdash-style session viewer across every runner and login:
 * a tree of emdash projects (repos and agents) with their sessions, each row
 * showing what the server reports about it, and the open session on the right.
 *
 * It shows data, not verdicts: every filter is a plain on/off toggle over a
 * reported field, and nothing ranks one session above another.
 */

const PREFS_KEY = 'canopy.ide.v1'
const POLL_MS = 15_000
// The list endpoint's ceiling (no paging). Asked for in full so archived
// sessions are not silently cut at the default 200; hitting it is said aloud.
const LIST_LIMIT = 500
// A runner reports every ~10s per box, and each report rings the doorbell; one
// refetch per window is plenty.
const DOORBELL_MIN_MS = 3_000
// Rail width, dragged by its right edge; double-click the edge to reset.
const RAIL_DEFAULT = 400
const RAIL_MIN = 260
const RAIL_MAX = 900
const clampRail = (w: number) => Math.round(Math.min(RAIL_MAX, Math.max(RAIL_MIN, w)))

type Prefs = { filters: Omit<IdeFilters, 'query'>; collapsed: string[]; railWidth: number; railHidden: boolean }

function loadPrefs(): Prefs {
  const fallback: Prefs = { filters: { ...DEFAULT_FILTERS }, collapsed: [], railWidth: RAIL_DEFAULT, railHidden: false }
  try {
    const raw = window.localStorage.getItem(PREFS_KEY)
    if (!raw) return fallback
    const p = JSON.parse(raw) as Partial<Prefs>
    return {
      filters: {
        ...DEFAULT_FILTERS,
        ...p.filters,
        states: { ...DEFAULT_FILTERS.states, ...p.filters?.states },
        modes: { ...DEFAULT_FILTERS.modes, ...p.filters?.modes },
      },
      collapsed: Array.isArray(p.collapsed) ? p.collapsed : [],
      railWidth: typeof p.railWidth === 'number' ? clampRail(p.railWidth) : RAIL_DEFAULT,
      railHidden: p.railHidden === true,
    }
  } catch {
    return fallback
  }
}

function savePrefs(p: Prefs) {
  try {
    window.localStorage.setItem(PREFS_KEY, JSON.stringify(p))
  } catch {
    // storage disabled — prefs last for this tab only
  }
}

/** Refetch whenever a runner reports (ws/supervisor/ frames), so a session
 *  changing state shows within seconds; the poll is the fallback. */
function useDoorbell(ring: () => void): boolean {
  const ringRef = useRef(ring)
  ringRef.current = ring
  const [connected, setConnected] = useState(false)
  useEffect(() => {
    let ws: WebSocket | null = null
    let timer: ReturnType<typeof setTimeout> | null = null
    let closed = false
    let attempt = 0
    const connect = () => {
      if (closed) return
      ws = new WebSocket(wsUrl('ws/supervisor/'))
      ws.onopen = () => {
        attempt = 0
        setConnected(true)
      }
      ws.onmessage = () => ringRef.current()
      ws.onclose = () => {
        setConnected(false)
        if (closed) return
        const backoff = [1000, 2000, 5000, 10000][Math.min(attempt++, 3)]
        timer = setTimeout(connect, backoff)
      }
    }
    connect()
    return () => {
      closed = true
      if (timer) clearTimeout(timer)
      ws?.close()
    }
  }, [])
  return connected
}

type RunnerChip = { key: string; name: string; status: string | null; host: string; count: number }

const STATE_LABEL: Record<IdeState, string> = { running: 'running', input: 'input', idle: 'idle' }
const MODE_LABEL: Record<IdeMode, string> = { manual: 'manual', auto: 'auto', none: 'no turn' }

function StateDot({ state }: { state: IdeState }) {
  return (
    <span
      aria-hidden
      className={clsx(
        'inline-block h-2 w-2 shrink-0 rounded-full',
        state === 'running' && 'animate-pulse bg-success',
        state === 'input' && 'bg-warning',
        state === 'idle' && 'bg-muted-foreground/40',
      )}
    />
  )
}

function ModeTag({ mode }: { mode: IdeMode }) {
  return (
    <span
      title={mode === 'none' ? 'No canopy turn has been claimed for this session' : `${mode} mode`}
      className={clsx(
        'w-4 shrink-0 rounded text-center text-[11px] font-semibold leading-4',
        mode === 'manual' && 'bg-muted text-foreground',
        mode === 'auto' && 'bg-primary/10 text-primary',
        mode === 'none' && 'text-muted-foreground',
      )}
    >
      {mode === 'manual' ? 'M' : mode === 'auto' ? 'A' : '–'}
    </span>
  )
}

function runnerOnline(status: string | null): boolean {
  return status === 'online'
}

export function IdePage() {
  const { id } = useParams()
  const navigate = useNavigate()
  const [initial] = useState(loadPrefs)
  const [filters, setFilters] = useState<IdeFilters>({ ...initial.filters, query: '' })
  const [collapsed, setCollapsed] = useState<Set<string>>(() => new Set(initial.collapsed))
  const [sessions, setSessions] = useState<ChatSession[]>([])
  const [runners, setRunners] = useState<RunnerOut[]>([])
  const [agents, setAgents] = useState<AgentOut[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [now, setNow] = useState(() => new Date())
  const lastFetch = useRef(0)
  const pending = useRef<ReturnType<typeof setTimeout> | null>(null)
  const [railWidth, setRailWidth] = useState(initial.railWidth)
  const [railHidden, setRailHidden] = useState(initial.railHidden)
  const appBarHidden = useAppBarHidden()
  const searchRef = useRef<HTMLInputElement>(null)
  const [menu, setMenu] = useState<{ session: ChatSession; x: number; y: number } | null>(null)
  const [note, setNote] = useState<{ text: string; error: boolean } | null>(null)
  // Closes relayed to a runner, by session id → when asked. The row stays,
  // marked, until the runner's report retires it (see settleClosing).
  const [closing, setClosing] = useState<Record<string, number>>({})
  const closingRef = useRef(closing)
  useEffect(() => {
    closingRef.current = closing
  }, [closing])

  useEffect(() => {
    const { hiddenRunners, states, modes, archived } = filters
    savePrefs({ filters: { hiddenRunners, states, modes, archived }, collapsed: [...collapsed], railWidth, railHidden })
  }, [filters, collapsed, railWidth, railHidden])

  const state = filters.archived ? 'all' : 'active'
  const refresh = useCallback(() => {
    lastFetch.current = Date.now()
    return Promise.allSettled([listSessions(state, { limit: LIST_LIMIT }), listRunners()]).then(([s, r]) => {
      if (s.status === 'fulfilled') {
        setSessions(s.value)
        setError(null)
        if (Object.keys(closingRef.current).length) {
          const settled = settleClosing(closingRef.current, s.value, Date.now())
          setClosing(settled.pending)
          if (settled.stuck.length || settled.failed.length) {
            setNote({ text: 'A delete was not confirmed by its runner — try again, or delete it in emdash.', error: true })
          }
        }
      } else {
        setError(s.reason instanceof Error ? s.reason.message : 'failed to load sessions')
      }
      if (r.status === 'fulfilled') setRunners(r.value ?? [])
      setNow(new Date())
      setLoading(false)
    })
  }, [state])

  useEffect(() => {
    void refresh()
    const poll = window.setInterval(() => void refresh(), POLL_MS)
    const onFocus = () => void refresh()
    window.addEventListener('focus', onFocus)
    return () => {
      window.clearInterval(poll)
      window.removeEventListener('focus', onFocus)
    }
  }, [refresh])

  // The agents "New session" offers; repos come from the runners' reports.
  useEffect(() => {
    let live = true
    listAgents({ limit: 100 })
      .then((page) => { if (live) setAgents(page.items) })
      .catch(() => { /* repos alone still make a usable menu */ })
    return () => {
      live = false
    }
  }, [])

  useEffect(() => {
    const t = window.setInterval(() => setNow(new Date()), 30_000)
    return () => window.clearInterval(t)
  }, [])

  // ⌘B / Ctrl-B hides the rail; "/" jumps to the filter box (not while typing).
  useEffect(() => {
    const onKey = (e: globalThis.KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && !e.shiftKey && !e.altKey && e.key.toLowerCase() === 'b') {
        e.preventDefault()
        setRailHidden((h) => !h)
        return
      }
      const t = e.target as HTMLElement | null
      const typing = t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName))
      if (e.key === '/' && !typing && !e.metaKey && !e.ctrlKey && !e.altKey) {
        e.preventDefault()
        setRailHidden(false)
        requestAnimationFrame(() => searchRef.current?.focus())
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  const ring = useCallback(() => {
    if (pending.current) return
    const wait = Math.max(0, lastFetch.current + DOORBELL_MIN_MS - Date.now())
    pending.current = setTimeout(() => {
      pending.current = null
      void refresh()
    }, wait)
  }, [refresh])
  const live = useDoorbell(ring)

  // Every runner you can see, plus any a visible session is bound to (a
  // teammate's box you cannot list), plus "no runner" for web chats.
  const chips = useMemo<RunnerChip[]>(() => {
    const byKey = new Map<string, RunnerChip>()
    for (const r of runners) {
      if (r.status === 'retired') continue
      byKey.set(r.id, { key: r.id, name: r.name, status: r.status, host: r.host, count: 0 })
    }
    for (const s of sessions) {
      if (!filters.archived && s.status === 'archived') continue
      const key = runnerKey(s)
      const chip =
        byKey.get(key) ??
        (key === NO_RUNNER
          ? { key, name: 'no runner', status: null, host: '', count: 0 }
          : { key, name: s.runner_name ?? key, status: s.runner_status ?? null, host: s.runner_host ?? '', count: 0 })
      chip.count += 1
      byKey.set(key, chip)
    }
    return [...byKey.values()].sort(
      (a, b) => Number(b.key !== NO_RUNNER) - Number(a.key !== NO_RUNNER) || a.name.localeCompare(b.name),
    )
  }, [runners, sessions, filters.archived])

  const tree = useMemo(() => buildTree(sessions, filters), [sessions, filters])
  const shown = useMemo(() => tree.flatMap((f) => f.sessions), [tree])
  // Sessions the filters keep out of the tree. Said in the list itself, with a
  // way back: a runner switched off weeks ago otherwise reads as "no sessions".
  const filteredOut = sessions.filter((s) => filters.archived || s.status !== 'archived').length - shown.length
  const showAll = () =>
    setFilters((f) => ({ ...DEFAULT_FILTERS, archived: f.archived }))
  // The agent panel's close review reads what is on screen, as you.
  useCloseReview(shown, state)
  const totals = useMemo(() => {
    const t: Record<IdeState, number> = { running: 0, input: 0, idle: 0 }
    for (const f of tree) for (const k of Object.keys(t) as IdeState[]) t[k] += f.counts[k]
    return t
  }, [tree])

  const toggleRunner = (key: string) =>
    setFilters((f) => ({
      ...f,
      hiddenRunners: f.hiddenRunners.includes(key) ? f.hiddenRunners.filter((k) => k !== key) : [...f.hiddenRunners, key],
    }))
  // Alt-click: show only this runner, or everything again if it already is.
  const soloRunner = (key: string) =>
    setFilters((f) => {
      const others = chips.map((c) => c.key).filter((k) => k !== key)
      const alreadySolo = !f.hiddenRunners.includes(key) && others.every((k) => f.hiddenRunners.includes(k))
      return { ...f, hiddenRunners: alreadySolo ? [] : others }
    })
  const toggleState = (k: IdeState) => setFilters((f) => ({ ...f, states: { ...f.states, [k]: !f.states[k] } }))
  const toggleMode = (k: IdeMode) => setFilters((f) => ({ ...f, modes: { ...f.modes, [k]: !f.modes[k] } }))
  const toggleFolder = (key: string) =>
    setCollapsed((prev) => {
      const next = new Set(prev)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return next
    })

  const allCollapsed = tree.length > 0 && tree.every((f) => collapsed.has(f.key))
  const setAllCollapsed = (on: boolean) => setCollapsed(on ? new Set(tree.map((f) => f.key)) : new Set())

  // Up/down walks the visible rows while focus is in the tree.
  const onTreeKey = (e: KeyboardEvent<HTMLDivElement>) => {
    if (e.key !== 'ArrowDown' && e.key !== 'ArrowUp') return
    const visible = tree.flatMap((f) => (collapsed.has(f.key) ? [] : f.sessions))
    if (!visible.length) return
    e.preventDefault()
    const i = visible.findIndex((s) => s.id === id)
    const next = e.key === 'ArrowDown' ? Math.min(visible.length - 1, i + 1) : Math.max(0, i < 0 ? 0 : i - 1)
    navigate(`/ide/${visible[next].id}`)
  }

  const startDrag = (e: ReactPointerEvent<HTMLDivElement>) => {
    e.preventDefault()
    const startX = e.clientX
    const startW = railWidth
    const move = (ev: PointerEvent) => setRailWidth(clampRail(startW + ev.clientX - startX))
    const up = () => {
      window.removeEventListener('pointermove', move)
      window.removeEventListener('pointerup', up)
      document.body.style.removeProperty('cursor')
      document.body.style.removeProperty('user-select')
    }
    document.body.style.cursor = 'col-resize'
    document.body.style.userSelect = 'none'
    window.addEventListener('pointermove', move)
    window.addEventListener('pointerup', up)
  }

  const dismissMenu = useCallback(() => setMenu(null), [])
  const label = (s: ChatSession) => sessionDisplayTitle(s.title) || '(untitled)'
  const failed = (e: unknown, fallback: string) =>
    setNote({ text: e instanceof Error ? e.message : fallback, error: true })

  const runAction = async (s: ChatSession, a: IdeAction) => {
    setMenu(null)
    const url = `${window.location.origin}/ide/${s.id}`
    try {
      switch (a.kind) {
        case 'open-tab':
          window.open(url, '_blank', 'noopener')
          return
        case 'copy-link':
          await navigator.clipboard.writeText(url)
          setNote({ text: 'Link copied', error: false })
          return
        case 'open-desktop':
          window.location.href = a.href
          setNote({ text: `Opening “${label(s)}” in Claude desktop`, error: false })
          return
        case 'stop': {
          const r = await stopSession(s.id)
          setNote({ text: r.cancelled ? `Stopped “${label(s)}”` : `Asked ${s.runner_name ?? 'its runner'} to interrupt “${label(s)}”`, error: false })
          break
        }
        case 'transfer': {
          const r = await transferSession(s.id, a.runner.id)
          setNote({
            text:
              r.status === 'moved'
                ? `Moved “${label(s)}” to ${r.runner || a.runner.name}`
                : `Transfer to ${a.runner.name} is waiting on ${r.approvers?.join(', ') || 'its admin'}`,
            error: false,
          })
          break
        }
        case 'archive':
          await archiveSession(s.id)
          setNote({ text: `Archived “${label(s)}”`, error: false })
          break
        case 'unarchive':
          await unarchiveSession(s.id)
          setNote({ text: `Unarchived “${label(s)}”`, error: false })
          break
        case 'close': {
          const working = s.running ? ' It is still working.' : ''
          if (!window.confirm(`Delete “${label(s)}”?${working} ${deleteConsequence(s)}`)) return
          const r = await closeSession(s.id)
          const message = closeResultMessage(r, s)
          if (message) {
            setNote({ text: message, error: true })
            return
          }
          if (r.closing) setClosing((prev) => ({ ...prev, [s.id]: Date.now() }))
          setNote({ text: r.closing ? `Deleting “${label(s)}” on ${s.runner_name}…` : `Deleted “${label(s)}”`, error: false })
          if (s.id === id && !r.closing) navigate('/ide', { replace: true })
          break
        }
      }
      void refresh()
    } catch (e) {
      failed(e, 'That did not work')
    }
  }

  // Runners and modes switched off — the state filters show themselves above.
  const hiddenCount =
    filters.hiddenRunners.filter((k) => chips.some((c) => c.key === k)).length +
    Object.values(filters.modes).filter((on) => !on).length

  const header = (
    <div className="flex flex-col gap-1 px-2 py-1.5">
      <div className="flex items-center gap-1.5">
        <button
          type="button"
          onClick={() => setAppBarHidden(!appBarHidden)}
          aria-pressed={appBarHidden}
          title={appBarHidden ? 'Show the app bar' : 'Hide the app bar for more room'}
          data-testid="ide-appbar-toggle"
          className="flex h-6 w-6 shrink-0 items-center justify-center rounded text-muted-foreground hover:bg-muted hover:text-foreground"
        >
          <Menu className="h-3.5 w-3.5" aria-hidden />
        </button>
        <span className="text-[13px] font-semibold text-foreground">IDE</span>
        <span className="text-[11px] tabular-nums text-muted-foreground" data-testid="ide-totals">
          {shown.length}
        </span>
        {/* The totals ARE the state filters: a count struck through is hidden. */}
        <div className="flex min-w-0 items-center gap-0.5">
          {(['running', 'input', 'idle'] as IdeState[]).map((k) => (
            <button
              key={k}
              type="button"
              aria-pressed={filters.states[k]}
              onClick={() => toggleState(k)}
              data-testid={`ide-state-${k}`}
              title={`${filters.states[k] ? 'Hide' : 'Show'} ${STATE_LABEL[k]} sessions`}
              className={clsx(
                'inline-flex items-center gap-1 rounded px-1 py-0.5 text-[11px] tabular-nums hover:bg-muted',
                filters.states[k]
                  ? 'text-foreground-secondary'
                  : 'text-muted-foreground line-through decoration-muted-foreground/50',
              )}
            >
              <StateDot state={k} />
              {totals[k]} {STATE_LABEL[k]}
            </button>
          ))}
        </div>
        {sessions.length >= LIST_LIMIT && (
          <span
            className="text-[11px] text-warning"
            title={`The list returns at most ${LIST_LIMIT} sessions per organisation; older ones are not shown.`}
          >
            capped
          </span>
        )}
        <NewChatMenu
          agents={agents}
          runners={runners}
          label="New session"
          className="ml-auto h-6 shrink-0 px-1.5 text-[12px]"
          onError={(message) => setNote({ text: message, error: true })}
          onCreated={(s) => {
            void refresh()
            navigate(`/ide/${s.id}`)
          }}
        />
        <span
          aria-label={live ? 'live' : 'polling'}
          className={clsx('shrink-0 text-[11px]', live ? 'text-success' : 'text-muted-foreground')}
          title={live ? 'Updates as runners report' : `Live updates unavailable — polling every ${POLL_MS / 1000}s`}
        >
          {live ? '●' : '○'}
        </span>
      </div>

      <div className="flex items-center gap-1">
        <input
          type="search"
          value={filters.query}
          onChange={(e) => setFilters((f) => ({ ...f, query: e.target.value }))}
          placeholder="Filter by title, project or runner"
          ref={searchRef}
          data-testid="ide-search"
          className="h-6 min-w-0 flex-1 rounded border border-input bg-background px-2 text-[12px] text-foreground placeholder:text-muted-foreground"
        />
        <DropdownMenu>
          <DropdownMenuTrigger
            data-testid="ide-filters"
            className={clsx(
              'flex h-6 shrink-0 items-center gap-1 rounded border px-1.5 text-[12px] hover:bg-muted',
              hiddenCount || filters.archived
                ? 'border-primary/40 bg-primary/10 text-foreground'
                : 'border-border text-muted-foreground',
            )}
          >
            {/* Say how much is filtered out even while the menu is shut — a
                hidden filter that silently narrows the list is worse than clutter. */}
            Filters{hiddenCount ? ` · ${hiddenCount} off` : ''}
            {filters.archived ? ' · +archived' : ''}
            <ChevronDown className="h-3 w-3 opacity-60" aria-hidden />
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end" className="max-h-[70vh] w-64 overflow-y-auto">
            <DropdownMenuLabel className="text-[11px] font-normal text-muted-foreground">
              Runners · Alt-click to show only one
            </DropdownMenuLabel>
            {chips.map((c) => (
              <DropdownMenuCheckboxItem
                key={c.key}
                checked={!filters.hiddenRunners.includes(c.key)}
                onClick={(e) => (e.altKey ? soloRunner(c.key) : toggleRunner(c.key))}
                data-testid={`ide-runner-${c.key}`}
                title={[c.name, c.host, c.status].filter(Boolean).join(' · ')}
                className="text-[12px]"
              >
                {c.key !== NO_RUNNER && (
                  <span
                    aria-hidden
                    className={clsx(
                      'mr-1.5 h-1.5 w-1.5 shrink-0 rounded-full',
                      runnerOnline(c.status) ? 'bg-success' : 'bg-muted-foreground/50',
                    )}
                  />
                )}
                <span className="min-w-0 flex-1 truncate">{c.name}</span>
                <span className="ml-2 text-[11px] tabular-nums text-muted-foreground">{c.count}</span>
              </DropdownMenuCheckboxItem>
            ))}
            <DropdownMenuSeparator />
            <DropdownMenuLabel className="text-[11px] font-normal text-muted-foreground">Mode</DropdownMenuLabel>
            {(['manual', 'auto', 'none'] as IdeMode[]).map((k) => (
              <DropdownMenuCheckboxItem
                key={k}
                checked={filters.modes[k]}
                onClick={() => toggleMode(k)}
                data-testid={`ide-mode-${k}`}
                className="text-[12px]"
              >
                {MODE_LABEL[k]}
              </DropdownMenuCheckboxItem>
            ))}
            <DropdownMenuSeparator />
            <DropdownMenuCheckboxItem
              checked={filters.archived}
              onClick={() => setFilters((f) => ({ ...f, archived: !f.archived }))}
              data-testid="ide-archived"
              className="text-[12px]"
            >
              Include archived
            </DropdownMenuCheckboxItem>
          </DropdownMenuContent>
        </DropdownMenu>
        <DropdownMenu>
          <DropdownMenuTrigger
            aria-label="More"
            data-testid="ide-more"
            className="flex h-6 w-6 shrink-0 items-center justify-center rounded text-muted-foreground hover:bg-muted hover:text-foreground"
          >
            <MoreHorizontal className="h-3.5 w-3.5" aria-hidden />
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end" className="w-48">
            <DropdownMenuItem className="text-[12px]" onClick={() => setAllCollapsed(!allCollapsed)}>
              {allCollapsed ? 'Expand all projects' : 'Collapse all projects'}
            </DropdownMenuItem>
            <DropdownMenuItem className="text-[12px]" onClick={() => setRailHidden(true)}>
              Hide the session list
              <DropdownMenuShortcut>⌘B</DropdownMenuShortcut>
            </DropdownMenuItem>
            <DropdownMenuItem className="text-[12px]" onClick={() => setAppBarHidden(!appBarHidden)}>
              {appBarHidden ? 'Show the app bar' : 'Hide the app bar'}
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </div>
      {note && (
        <div
          className={clsx('flex items-start gap-2 text-[11px]', note.error ? 'text-destructive' : 'text-muted-foreground')}
          data-testid="ide-note"
        >
          <span className="min-w-0 flex-1">{note.text}</span>
          <button type="button" onClick={() => setNote(null)} className="shrink-0 hover:text-foreground" aria-label="Dismiss">
            ×
          </button>
        </div>
      )}
    </div>
  )

  return (
    <WorkbenchShell>
      {!railHidden && (
      <div className="relative flex min-h-0 shrink-0" style={{ width: railWidth }} data-testid="ide-rail">
      <WorkbenchRail width="md:w-full" className="min-w-0 flex-1" header={header}>
        <div className="py-1 text-[13px]" onKeyDown={onTreeKey} data-testid="ide-tree">
          {loading && sessions.length === 0 && <p className="px-3 py-2 text-muted-foreground">Loading…</p>}
          {error && <p className="px-3 py-2 text-destructive">{error}</p>}
          {!loading && tree.length === 0 && !error && (
            <p className="px-3 py-2 text-muted-foreground">No sessions match these filters.</p>
          )}
          {tree.map((folder) => {
            const open = !collapsed.has(folder.key)
            return (
              <div key={folder.key || '(none)'}>
                <button
                  type="button"
                  onClick={() => toggleFolder(folder.key)}
                  aria-expanded={open}
                  className="flex w-full items-center gap-1.5 px-2 py-1 text-left font-semibold text-foreground-secondary hover:bg-muted"
                >
                  <span className="w-3 text-muted-foreground">{open ? '▾' : '▸'}</span>
                  <span className="truncate">{folder.key || 'No project'}</span>
                  {folder.isAgent && <span className="font-normal text-muted-foreground">agent</span>}
                  <span className="ml-auto flex shrink-0 items-center gap-2 font-normal text-[11px] text-muted-foreground">
                    {folder.counts.running > 0 && (
                      <span className="flex items-center gap-1" title="running">
                        <StateDot state="running" />
                        {folder.counts.running}
                      </span>
                    )}
                    {folder.counts.input > 0 && (
                      <span className="flex items-center gap-1" title="waiting for input">
                        <StateDot state="input" />
                        {folder.counts.input}
                      </span>
                    )}
                    <span title="sessions shown">{folder.sessions.length}</span>
                  </span>
                </button>
                {open &&
                  folder.sessions.map((s) => {
                    const st = sessionState(s)
                    const title = label(s)
                    const deleting = s.id in closing
                    return (
                      <Link
                        key={s.id}
                        to={`/ide/${s.id}`}
                        data-testid={`ide-row-${s.id}`}
                        aria-current={s.id === id ? 'page' : undefined}
                        onContextMenu={(e) => {
                          e.preventDefault()
                          setMenu({ session: s, x: e.clientX, y: e.clientY })
                        }}
                        title={[
                          s.title,
                          `${STATE_LABEL[st]} · ${MODE_LABEL[sessionMode(s)]}`,
                          s.runner_name ? `runner ${s.runner_name}${s.runner_host ? ` (${s.runner_host})` : ''}` : 'no runner',
                          `workspace ${s.workspace}`,
                          `last activity ${new Date(s.last_activity_at).toLocaleString()}`,
                          s.status === 'archived' ? 'archived' : '',
                        ]
                          .filter(Boolean)
                          .join('\n')}
                        className={clsx(
                          'grid grid-cols-[8px_minmax(0,1fr)_auto_auto_2.25rem] items-center gap-2 py-1 pl-7 pr-2',
                          s.id === id ? 'bg-primary/10 text-foreground' : 'text-foreground-secondary hover:bg-muted',
                          (s.status === 'archived' || deleting) && 'opacity-60',
                          menu?.session.id === s.id && 'ring-1 ring-inset ring-primary/50',
                        )}
                      >
                        <StateDot state={st} />
                        <span className="truncate">{deleting ? `Deleting… ${title}` : title}</span>
                        <span className="max-w-[140px] truncate rounded border border-border px-1 text-[11px] text-muted-foreground">
                          {s.runner_name ?? 'web'}
                        </span>
                        <ModeTag mode={sessionMode(s)} />
                        <span
                          className={clsx(
                            'text-right text-[11px] tabular-nums',
                            st === 'running' ? 'text-success' : 'text-muted-foreground',
                          )}
                        >
                          {deleting ? '…' : st === 'running' ? 'live' : shortAge(s.last_activity_at, now)}
                        </span>
                      </Link>
                    )
                  })}
              </div>
            )
          })}
          {filteredOut > 0 && (
            <p className="px-3 py-2 text-[11px] text-muted-foreground" data-testid="ide-filtered-out">
              {filteredOut} {filteredOut === 1 ? 'session' : 'sessions'} hidden by filters ·{' '}
              <button type="button" onClick={showAll} className="underline hover:text-foreground" data-testid="ide-show-all">
                Show all
              </button>
            </p>
          )}
        </div>
      </WorkbenchRail>
      <div
        role="separator"
        aria-orientation="vertical"
        aria-label="Resize the session list"
        title="Drag to resize · double-click to reset"
        data-testid="ide-rail-resize"
        onPointerDown={startDrag}
        onDoubleClick={() => setRailWidth(RAIL_DEFAULT)}
        className="absolute inset-y-0 -right-1 z-10 w-2 cursor-col-resize hover:bg-primary/30 active:bg-primary/40"
      />
      </div>
      )}
      {railHidden && (
        <button
          type="button"
          onClick={() => setRailHidden(false)}
          title="Show the session list (⌘B)"
          aria-label="Show the session list"
          data-testid="ide-rail-show"
          className="flex w-5 shrink-0 items-start justify-center border-r border-border pt-2 text-[11px] text-muted-foreground hover:bg-muted hover:text-foreground"
        >
          ›
        </button>
      )}
      <div className="ide-dense min-h-0 min-w-0 flex-1">
        {id ? (
          <ChatPage
            key={id}
            compact
            onLeave={() => navigate('/ide', { replace: true })}
            sessionPath={(s) => `/ide/${encodeURIComponent(s.id)}`}
          />
        ) : (
          <div className="flex h-full items-center justify-center p-8 text-sm text-muted-foreground">
            Pick a session on the left.
          </div>
        )}
      </div>
      {menu && (
        <SessionContextMenu
          session={menu.session}
          x={menu.x}
          y={menu.y}
          runners={runners}
          onAction={(a) => void runAction(menu.session, a)}
          onDismiss={dismissMenu}
        />
      )}
    </WorkbenchShell>
  )
}

export default IdePage
