import { Link } from 'react-router-dom'
import { clsx } from 'clsx'
import type { KeyboardEvent } from 'react'

import { setSessionTitle, type ChatSession } from '@/api/chat'
import type { AgentOut } from '@/api/agents'
import type { RunnerOut } from '@/api/harness'
import { NewChatMenu, type NewChatTarget } from '@/components/chat/NewChatMenu'
import { TitleInput } from '@/components/chat/TitleInput'
import { sessionDisplayTitle } from '@/components/chat/sessionDisplayTitle'
import { listTitle, PrivateMark } from '@/components/chat/privateConversation'
import { StateDot as SharedStateDot } from '@/components/sessions/StateDot'
import {
  linkKind,
  sessionMode,
  sessionState,
  shortAge,
  type IdeFolder,
  type IdeMode,
  type IdeState,
} from './ideTree'

export const STATE_LABEL: Record<IdeState, string> = { running: 'running', input: 'input', idle: 'idle' }
export const MODE_LABEL: Record<IdeMode, string> = { manual: 'manual', auto: 'auto', none: 'no turn' }

export const sessionLabel = (s: ChatSession) =>
  listTitle(s, sessionDisplayTitle(s.title) || '(untitled)')

export function StateDot({ state }: { state: IdeState }) {
  return <SharedStateDot tone={state === 'input' ? 'asking' : state} />
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

export interface IdeTreeProps {
  /** The open session. */
  id: string | undefined
  tree: IdeFolder<ChatSession>[]
  sessions: ChatSession[]
  loading: boolean
  error: string | null
  now: Date
  collapsed: Set<string>
  toggleFolder: (key: string) => void
  /** What a folder's + starts a session with; null hides the +. */
  folderTarget: (key: string, isAgent: boolean) => NewChatTarget | null
  agents: AgentOut[]
  runners: RunnerOut[]
  onCreated: (s: ChatSession) => void
  onError: (message: string) => void
  /** Sessions whose delete is waiting on its runner. */
  closing: Record<string, number>
  /** Sessions started from each session, by parent id. */
  links: Map<string, ChatSession[]>
  collapsedLinks: Set<string>
  toggleLinks: (sessionId: string) => void
  /** The row whose context menu is open (ringed). */
  menuSessionId: string | undefined
  onRowMenu: (s: ChatSession, x: number, y: number) => void
  renamingId: string | null
  onRename: (sessionId: string | null) => void
  /** A row changed here (renamed): refetch. */
  onChanged: () => void
  filteredOut: number
  showAll: () => void
  onKeyDown: (e: KeyboardEvent<HTMLDivElement>) => void
}

/** /ide's session tree: a folder per emdash project, its sessions as rows, and
 *  under a row the sessions started from it. Presentational — every change
 *  goes back through the callbacks. */
export function IdeTree({
  id,
  tree,
  sessions,
  loading,
  error,
  now,
  collapsed,
  toggleFolder,
  folderTarget,
  agents,
  runners,
  onCreated,
  onError,
  closing,
  links,
  collapsedLinks,
  toggleLinks,
  menuSessionId,
  onRowMenu,
  renamingId,
  onRename,
  onChanged,
  filteredOut,
  showAll,
  onKeyDown,
}: IdeTreeProps) {
  const label = sessionLabel
  return (
    <div className="py-1 text-[13px]" onKeyDown={onKeyDown} data-testid="ide-tree">
      {loading && sessions.length === 0 && <p className="px-3 py-2 text-muted-foreground">Loading…</p>}
      {error && <p className="px-3 py-2 text-destructive">{error}</p>}
      {!loading && tree.length === 0 && !error && (
        <p className="px-3 py-2 text-muted-foreground">No sessions match these filters.</p>
      )}
      {tree.map((folder) => {
        const open = !collapsed.has(folder.key)
        const target = folderTarget(folder.key, folder.isAgent)
        return (
          <div key={folder.key || '(none)'}>
            <div className="flex items-center pr-2 hover:bg-muted">
            <button
              type="button"
              onClick={() => toggleFolder(folder.key)}
              aria-expanded={open}
              className="flex min-w-0 flex-1 items-center gap-1.5 py-1 pl-2 text-left font-semibold text-foreground-secondary"
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
            {target && (
              <NewChatMenu
                agents={agents}
                runners={runners}
                target={target}
                label=""
                title={`New session with ${folder.key}`}
                className="ml-1.5 h-5 w-5 shrink-0 p-0 text-muted-foreground hover:text-foreground"
                onError={onError}
                onCreated={onCreated}
              />
            )}
            </div>
            {open &&
              folder.sessions.map((s) => {
                const st = sessionState(s)
                const title = label(s)
                const deleting = s.id in closing
                const parent = s.parent_session_id ? sessions.find((p) => p.id === s.parent_session_id) : undefined
                const kids = links.get(s.id) ?? []
                const kidsOpen = !collapsedLinks.has(s.id)
                return (
                  <div key={s.id} className="relative">
                  {kids.length > 0 && (
                    <button
                      type="button"
                      onClick={() => toggleLinks(s.id)}
                      aria-expanded={kidsOpen}
                      aria-label={kidsOpen ? 'Hide the sessions started from this one' : 'Show the sessions started from this one'}
                      title={`${kids.length} ${kids.length === 1 ? 'session' : 'sessions'} started from this one`}
                      data-testid={`ide-links-toggle-${s.id}`}
                      className="absolute left-2 top-0 z-[1] flex h-[26px] w-4 items-center justify-center text-[11px] text-muted-foreground hover:text-foreground"
                    >
                      {kidsOpen ? '▾' : '▸'}
                    </button>
                  )}
                  <Link
                    to={`/ide/${s.id}`}
                    data-testid={`ide-row-${s.id}`}
                    aria-current={s.id === id ? 'page' : undefined}
                    onContextMenu={(e) => {
                      e.preventDefault()
                      onRowMenu(s, e.clientX, e.clientY)
                    }}
                    onDoubleClick={(e) => {
                      e.preventDefault()
                      onRename(s.id)
                    }}
                    title={[
                      s.sensitive ? title : s.title,
                      parent ? `${linkKind(s) === 'fork' ? 'forked' : 'started'} from ${label(parent)}` : '',
                      `${STATE_LABEL[st]} · ${MODE_LABEL[sessionMode(s)]}`,
                      s.runner_name ? `runner ${s.runner_name}${s.runner_host ? ` (${s.runner_host})` : ''}` : 'no runner',
                      s.runner_signed_out ? `Claude isn't signed in on ${s.runner_name}` : '',
                      `workspace ${s.workspace}`,
                      `last activity ${new Date(s.last_activity_at).toLocaleString()}`,
                      s.status === 'archived' ? 'archived' : '',
                    ]
                      .filter(Boolean)
                      .join('\n')}
                    className={clsx(
                      'grid grid-cols-[8px_minmax(0,1fr)_auto_auto_2.25rem] items-center gap-2 py-1 pr-2',
                      s.id === id ? 'bg-primary/10 text-foreground' : 'text-foreground-secondary hover:bg-muted',
                      (s.status === 'archived' || deleting) && 'opacity-60',
                      menuSessionId === s.id && 'ring-1 ring-inset ring-primary/50',
                    )}
                    style={{ paddingLeft: '1.75rem' }}
                  >
                    <StateDot state={st} />
                    {renamingId === s.id ? (
                      <TitleInput
                        initial={s.title ?? ''}
                        onSave={async (next) => {
                          await setSessionTitle(s.id, next)
                          onRename(null)
                          onChanged()
                        }}
                        onCancel={() => onRename(null)}
                        className="h-5 text-[13px]"
                      />
                    ) : (
                      <span className="flex min-w-0 items-center gap-1">
                        {s.sensitive && <PrivateMark />}
                        <span className="truncate">{deleting ? `Deleting… ${title}` : title}</span>
                      </span>
                    )}
                    <span
                      className={clsx(
                        'max-w-[140px] truncate rounded border px-1 text-[11px]',
                        s.runner_signed_out ? 'border-warning/40 text-warning' : 'border-border text-muted-foreground',
                      )}
                      title={s.runner_signed_out ? `Claude isn't signed in on ${s.runner_name}: it runs nothing until someone signs in there` : undefined}
                      data-testid={s.runner_signed_out ? `ide-runner-signed-out-${s.id}` : undefined}
                    >
                      {s.runner_signed_out && '⚠ '}
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
                  {kidsOpen &&
                    kids.map((k) => {
                      const kst = sessionState(k)
                      return (
                        // A reference to the real session, wherever it sits: opening it is
                        // opening that row, which the reveal above then highlights.
                        <Link
                          key={k.id}
                          to={`/ide/${k.id}`}
                          data-testid={`ide-link-${s.id}-${k.id}`}
                          title={`${linkKind(k) === 'fork' ? 'Forked' : 'Started'} from this session · opens ${label(k)} where it lives`}
                          className={clsx(
                            'grid grid-cols-[minmax(0,1fr)_auto_2.25rem] items-center gap-2 py-0.5 pr-2 text-[12px] text-muted-foreground hover:bg-muted hover:text-foreground-secondary',
                            k.id === id && 'bg-primary/5',
                          )}
                          style={{ paddingLeft: '2.75rem' }}
                        >
                          <span className="truncate">
                            ↳ {linkKind(k) === 'fork' && <span className="text-muted-foreground/80">fork · </span>}
                            {label(k)}
                          </span>
                          <span className="flex items-center gap-1 text-[11px]">
                            <StateDot state={kst} />
                            {STATE_LABEL[kst]}
                          </span>
                          <span className="text-right text-[11px] tabular-nums">
                            {kst === 'running' ? 'live' : shortAge(k.last_activity_at, now)}
                          </span>
                        </Link>
                      )
                    })}
                  </div>
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
  )
}
