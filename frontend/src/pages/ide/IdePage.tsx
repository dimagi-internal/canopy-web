import { useCallback, useEffect, useMemo, useRef, useState, type KeyboardEvent, type PointerEvent as ReactPointerEvent } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { clsx } from 'clsx'
import { ChevronDown, Menu, MoreHorizontal } from 'lucide-react'
import { WorkbenchRail, WorkbenchShell } from 'canopy-ui'
import {
  Dialog,
  DialogContent,
  DialogTitle,
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
  stopSession,
  transferSession,
  unarchiveSession,
  type ChatSession,
} from '@/api/chat'
import { NewChatMenu, type NewChatTarget } from '@/components/chat/NewChatMenu'
import { repoChoices } from '@/components/chat/runnerEligibility'
import { copyText } from 'canopy-ui/lib'
import { ChatPage } from '@/pages/ChatPage'
import { closeResultMessage } from '@/components/chat/closeAction'
import { SessionContextMenu, type IdeAction } from './SessionContextMenu'
import { scopedPath } from '@/lib/scopedLinks'
import { SessionInfoPanel } from '@/components/chat/SessionInfoPanel'
import { AddRepoToRunnerDialog } from '@/components/supervisor/AddRepoToRunnerDialog'
import { useCloseReview } from './useCloseReview'
import { IdeTree, MODE_LABEL, STATE_LABEL, StateDot, sessionLabel } from './SessionTree'
import { LIST_LIMIT, POLL_MS, useIdeData } from './useIdeData'
import { RAIL_DEFAULT, clampRail, useIdePrefs } from './useIdePrefs'
import {
  DEFAULT_FILTERS,
  NO_RUNNER,
  buildTree,
  childLinks,
  deleteConsequence,
  runnerKey,
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

type RunnerChip = { key: string; name: string; status: string | null; host: string; count: number }

function runnerOnline(status: string | null): boolean {
  return status === 'online'
}

export function IdePage() {
  const { id } = useParams()
  const navigate = useNavigate()
  const {
    filters,
    setFilters,
    collapsed,
    setCollapsed,
    collapsedLinks,
    setCollapsedLinks,
    railWidth,
    setRailWidth,
    railHidden,
    setRailHidden,
  } = useIdePrefs()
  const state = filters.archived ? 'all' : 'active'
  const {
    sessions,
    runners,
    agents,
    loading,
    error,
    now,
    note,
    setNote,
    closing,
    trackClose,
    refresh,
    live,
  } = useIdeData(state)
  const appBarHidden = useAppBarHidden()
  const searchRef = useRef<HTMLInputElement>(null)
  const [menu, setMenu] = useState<{ session: ChatSession; x: number; y: number } | null>(null)

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
  }, [setRailHidden])

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
  // What each folder's + starts a session with: the agent of that name, else a
  // repo some online runner holds. A folder with neither gets no +.
  const repos = useMemo(() => repoChoices(runners), [runners])
  const folderTarget = (key: string, isAgent: boolean): NewChatTarget | null => {
    const agent = isAgent ? agents.find((a) => a.slug === key) : undefined
    if (agent) return { kind: 'agent', agent }
    const project = repos.find((r) => r.slug === key)
    return project ? { kind: 'project', project } : null
  }
  // A session made here opens with its title being named.
  const [justCreated, setJustCreated] = useState<string | null>(null)
  const [renamingId, setRenamingId] = useState<string | null>(null)
  // The session whose Session info dialog is open (the row menu's "Session info…").
  const [infoId, setInfoId] = useState<string | null>(null)
  // "Add repo": ask one of your laptops to clone a repo and add it to emdash.
  const [addRepo, setAddRepo] = useState(false)
  useEffect(() => {
    if (justCreated && id !== justCreated) setJustCreated(null)
  }, [id, justCreated])
  const onCreated = (s: ChatSession) => {
    void refresh()
    setJustCreated(s.id)
    navigate(`/ide/${s.id}`)
  }
  const shown = useMemo(() => tree.flatMap((f) => f.sessions), [tree])
  const links = useMemo(() => childLinks(sessions, filters), [sessions, filters])
  // A session opened here that the list does not hold yet — a fork just made
  // from the open chat — is fetched now, not on the next poll, and its folder
  // opened, so it shows highlighted in its place and linked under its parent.
  const fetchedFor = useRef<string | null>(null)
  useEffect(() => {
    if (!id || loading || fetchedFor.current === id || sessions.some((s) => s.id === id)) return
    fetchedFor.current = id
    void refresh()
  }, [id, loading, sessions, refresh])
  // Once per opened session (adjusted during render, not in an effect), so a
  // folder collapsed by hand afterwards stays collapsed.
  const openFolderOf = tree.find((f) => f.sessions.some((s) => s.id === id))?.key
  const [revealed, setRevealed] = useState<string | null>(null)
  if (id && openFolderOf !== undefined && revealed !== id) {
    setRevealed(id)
    if (collapsed.has(openFolderOf)) setCollapsed((c) => new Set([...c].filter((k) => k !== openFolderOf)))
  }
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
  const toggleLinks = (sessionId: string) =>
    setCollapsedLinks((prev) => {
      const next = new Set(prev)
      if (next.has(sessionId)) next.delete(sessionId)
      else next.add(sessionId)
      return next
    })
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
  const label = sessionLabel
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
          setNote(
            (await copyText(url))
              ? { text: 'Link copied', error: false }
              : { text: 'Could not copy — the clipboard is blocked here', error: true },
          )
          return
        case 'copy-join-link':
          setNote(
            (await copyText(`${window.location.origin}${scopedPath(s.workspace, `/sessions/${s.id}`)}`))
              ? { text: `Join link for “${label(s)}” copied`, error: false }
              : { text: 'Could not copy — the clipboard is blocked here', error: true },
          )
          return
        case 'rename':
          setRenamingId(s.id)
          return
        case 'info':
          setInfoId(s.id)
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
          if (r.closing) trackClose(s.id, `Deleting “${label(s)}” on ${s.runner_name ?? 'its runner'}…`, `Deleted “${label(s)}”`)
          else setNote({ text: `Deleted “${label(s)}”`, error: false })
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
                'inline-flex items-center gap-1 whitespace-nowrap rounded px-1 py-0.5 text-[11px] tabular-nums hover:bg-muted',
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
          onCreated={onCreated}
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
            {/* Here rather than in the header row: a second button there squeezed
                the state counts under it on a narrow rail. */}
            <DropdownMenuItem
              className="text-[12px]"
              onClick={() => setAddRepo(true)}
              title="Add a repo to one of your laptop runners, as an emdash project"
              data-testid="ide-add-repo"
            >
              Add repo…
            </DropdownMenuItem>
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
        <IdeTree
          id={id}
          tree={tree}
          sessions={sessions}
          loading={loading}
          error={error}
          now={now}
          collapsed={collapsed}
          toggleFolder={toggleFolder}
          folderTarget={folderTarget}
          agents={agents}
          runners={runners}
          onCreated={onCreated}
          onError={(message) => setNote({ text: message, error: true })}
          closing={closing}
          links={links}
          collapsedLinks={collapsedLinks}
          toggleLinks={toggleLinks}
          menuSessionId={menu?.session.id}
          onRowMenu={(session, x, y) => setMenu({ session, x, y })}
          renamingId={renamingId}
          onRename={setRenamingId}
          onChanged={() => void refresh()}
          filteredOut={filteredOut}
          showAll={showAll}
          onKeyDown={onTreeKey}
        />
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
            editTitle={id === justCreated}
            onTitleChange={() => void refresh()}
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
      {addRepo && (
        <AddRepoToRunnerDialog open onClose={() => setAddRepo(false)} runners={runners} agents={agents} />
      )}
      <Dialog open={infoId !== null} onOpenChange={(open: boolean) => !open && setInfoId(null)}>
        <DialogContent className="w-[calc(100%-2rem)] max-w-2xl bg-card">
          <DialogTitle className="text-sm font-semibold text-foreground">Session info</DialogTitle>
          {infoId && <SessionInfoPanel sessionId={infoId} />}
        </DialogContent>
      </Dialog>
    </WorkbenchShell>
  )
}

export default IdePage
