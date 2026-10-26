import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { Plus } from 'lucide-react'
import {
  Button,
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from 'canopy-ui/ui'
import { createSession, type ChatSession } from '@/api/chat'
import { getAgentPinnableRunners, listMyAgents, type AgentOut, type AgentPinnableRunnerOut } from '@/api/agents'
import { listRunners, type RunnerOut } from '@/api/harness'
import { onlineSessionCapableRunners, repoChoices, reportedRepos, runnerOptionLabel, type RepoChoice } from './runnerEligibility'
import { scopedPath } from '../../lib/scopedLinks'
import { cliLabel } from '../../lib/clis'

// The "Run on" picker's pending target — set when the user picks an agent or
// project from the menu, before they've confirmed a runner + Start.
export type NewChatTarget =
  | { kind: 'agent'; agent: AgentOut }
  | { kind: 'project'; project: RepoChoice }

/**
 * "+ New chat": pick an agent (or a repo, for an agentless project chat), pick
 * where it runs, start. CROSS-WORKSPACE — an agent chat is created in the
 * agent's own workspace, a repo chat in its runner's. One implementation, two
 * doors: the Sessions list and the supervisor feed.
 *
 * The repos offered are the ones the fleet's online, session-capable runners
 * report (`repoChoices`) — the set a project chat can actually be claimed on.
 * `agents` and `runners` may be passed by a caller that already loaded them;
 * otherwise the menu loads them itself (Home passes neither). `onCreated` replaces the default of opening the new
 * chat at `/w/<ws>/chat/<id>` (/ide opens it in place). `target` skips the
 * list and opens straight on the "Run on" step for that agent or repo (/ide's
 * per-project +); with no `label` the trigger is the + alone.
 */
export function NewChatMenu({
  agents: agentsProp,
  runners: runnersProp,
  onError,
  onCreated,
  label = 'New chat',
  target,
  title,
  className,
}: {
  agents?: readonly AgentOut[]
  runners?: readonly RunnerOut[] | null
  onError: (message: string) => void
  onCreated?: (session: ChatSession) => void
  label?: string
  target?: NewChatTarget
  title?: string
  className?: string
}) {
  const navigate = useNavigate()
  const [ownRunners, setOwnRunners] = useState<RunnerOut[] | null>(null)
  const [ownAgents, setOwnAgents] = useState<AgentOut[]>([])
  const agents = agentsProp ?? ownAgents
  const fleetRunners = runnersProp ?? ownRunners
  const projects = useMemo(() => repoChoices(fleetRunners ?? []), [fleetRunners])
  const [creating, setCreating] = useState(false)
  const [open, setOpen] = useState(false)
  const [pending, setPending] = useState<NewChatTarget | null>(null)
  const [agentRunnerOptions, setAgentRunnerOptions] = useState<AgentPinnableRunnerOut[]>([])
  const [runnersLoading, setRunnersLoading] = useState(false)
  const [selectedRunnerId, setSelectedRunnerId] = useState('')
  // "" = the agent's default CLI; offered only when it is set up for more than one.
  const [selectedCli, setSelectedCli] = useState('')

  useEffect(() => {
    if (runnersProp !== undefined) return
    let live = true
    listRunners()
      .then((r) => { if (live) setOwnRunners(r) })
      .catch(() => { if (live) setOwnRunners([]) /* agents alone still make a usable menu */ })
    return () => {
      live = false
    }
  }, [runnersProp])

  useEffect(() => {
    if (agentsProp !== undefined) return
    let live = true
    listMyAgents({ limit: 100 })
      .then((page) => { if (live) setOwnAgents(page.items) })
      .catch(() => { /* repos alone still make a usable menu */ })
    return () => {
      live = false
    }
  }, [agentsProp])

  const start = useCallback(
    (args: Parameters<typeof createSession>[0]) => {
      setCreating(true)
      createSession(args)
        .then((s) => {
          if (onCreated) {
            setCreating(false)
            setOpen(false)
            setPending(null)
            setSelectedRunnerId('')
            onCreated(s)
          } else {
            navigate(scopedPath(s.workspace, `/chat/${s.id}`))
          }
        })
        .catch((err: unknown) => {
          onError(err instanceof Error ? err.message : 'could not start chat')
          setCreating(false)
        })
    },
    [navigate, onError, onCreated],
  )

  // Reset the "Run on" step, e.g. when the menu closes.
  const resetPending = useCallback(() => {
    setPending(null)
    setSelectedRunnerId('')
    setSelectedCli('')
  }, [])

  // pickAgent is an event handler, not an effect, so there's no cleanup
  // function to cancel a stale in-flight fetch — a ref tracking the
  // currently-picked slug lets a late response check whether it's still
  // wanted before applying, so rapidly switching agents can't have a slower
  // earlier response overwrite a newer pick's runner options.
  const pickedAgentSlugRef = useRef<string | null>(null)

  const pickAgent = useCallback((agent: AgentOut) => {
    setSelectedRunnerId('')
    setSelectedCli('')
    setPending({ kind: 'agent', agent })
    setRunnersLoading(true)
    pickedAgentSlugRef.current = agent.slug
    // Every box the caller may choose for this agent, not just its runner order:
    // choosing one pins the chat there past the order and any strict rule, and
    // the server answers which boxes the placement gates would accept.
    getAgentPinnableRunners(agent.slug, agent.workspace ?? undefined)
      .then((options) => {
        if (pickedAgentSlugRef.current === agent.slug) setAgentRunnerOptions(options)
      })
      .catch(() => {
        if (pickedAgentSlugRef.current === agent.slug) setAgentRunnerOptions([])
      })
      .finally(() => {
        if (pickedAgentSlugRef.current === agent.slug) setRunnersLoading(false)
      })
  }, [])

  const pickProject = useCallback((project: RepoChoice) => {
    setSelectedRunnerId('')
    setPending({ kind: 'project', project })
  }, [])

  const confirmStart = useCallback(() => {
    if (!pending) return
    const runnerId = selectedRunnerId || undefined
    if (pending.kind === 'agent') {
      start({
        agentSlug: pending.agent.slug,
        workspace: pending.agent.workspace ?? undefined,
        runnerId,
        cli: selectedCli || undefined,
      })
    } else {
      start({ project: pending.project.slug, workspace: pending.project.workspace ?? undefined, runnerId })
    }
  }, [pending, selectedRunnerId, selectedCli, start])

  // Project chats route through the fleet-wide runner list, filtered to
  // online + sessions-capable (only those can execute a chat turn at all), to
  // the runners that hold the picked repo (no other box could claim it), and to
  // the ones you administer (choosing a box for a project chat needs that; the
  // server refuses any other).
  const pickedRepo = pending?.kind === 'project' ? pending.project.slug : null
  const projectRunnerOptions = useMemo(
    () => onlineSessionCapableRunners(fleetRunners ?? [])
      .filter((r) => pickedRepo !== null && reportedRepos(r).includes(pickedRepo) && r.can_administer),
    [fleetRunners, pickedRepo],
  )

  return (
    <DropdownMenu
      open={open}
      onOpenChange={(next: boolean) => {
        setOpen(next)
        if (!next) resetPending()
        else if (target?.kind === 'agent') pickAgent(target.agent)
        else if (target?.kind === 'project') pickProject(target.project)
      }}
    >
      <DropdownMenuTrigger
        render={
          <Button
            size="sm"
            variant={label ? 'default' : 'ghost'}
            disabled={creating || (!target && agents.length === 0 && projects.length === 0)}
            data-testid="new-chat"
            title={title}
            aria-label={label ? undefined : title}
            className={className}
          />
        }
      >
        <Plus className={label ? 'mr-1 h-4 w-4' : 'h-3.5 w-3.5'} />
        {label}
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="max-h-80 overflow-y-auto">
        {!pending ? (
          <>
            <DropdownMenuLabel>New chat with…</DropdownMenuLabel>
            {agents.length === 0 && projects.length === 0 && <DropdownMenuItem disabled>No agents available</DropdownMenuItem>}
            {agents.map((a) => (
              <DropdownMenuItem
                key={`${a.workspace}/${a.slug}`}
                closeOnClick={false}
                onClick={() => pickAgent(a)}
              >
                {a.name}
                {a.workspace ? <span className="ml-2 text-xs text-muted-foreground">{a.workspace}</span> : null}
              </DropdownMenuItem>
            ))}
            {projects.length > 0 && (
              <>
                <DropdownMenuSeparator />
                <DropdownMenuLabel>Repos</DropdownMenuLabel>
                {projects.map((p) => (
                  <DropdownMenuItem
                    key={`${p.workspace}/${p.slug}`}
                    closeOnClick={false}
                    onClick={() => pickProject(p)}
                  >
                    {p.slug}
                    {p.workspace ? <span className="ml-2 text-xs text-muted-foreground">{p.workspace}</span> : null}
                  </DropdownMenuItem>
                ))}
              </>
            )}
          </>
        ) : (
          <div className="flex flex-col gap-2 px-2 py-1.5" data-testid="run-on-picker">
            <div className="text-sm text-foreground">
              {pending.kind === 'agent' ? pending.agent.name : pending.project.slug}
            </div>
            <label className="flex flex-col gap-1 text-[11px] text-muted-foreground">
              Run on
              <select
                value={selectedRunnerId}
                onChange={(e) => setSelectedRunnerId(e.target.value)}
                disabled={runnersLoading}
                className="rounded-md border border-input bg-input px-1.5 py-1 text-[12px] text-foreground"
                data-testid="run-on-select"
              >
                <option value="">Auto</option>
                {pending.kind === 'agent'
                  ? <AgentRunnerOptions agent={pending.agent.name} runners={agentRunnerOptions} />
                  : projectRunnerOptions.map((r) => (
                      <option key={r.id} value={r.id}>
                        ● {runnerOptionLabel(r)}
                      </option>
                    ))}
              </select>
            </label>
            {pending.kind === 'agent' && (pending.agent.clis ?? []).length > 1 && (
              <label className="flex flex-col gap-1 text-[11px] text-muted-foreground">
                Agent CLI
                <select
                  value={selectedCli}
                  onChange={(e) => setSelectedCli(e.target.value)}
                  className="rounded-md border border-input bg-input px-1.5 py-1 text-[12px] text-foreground"
                  data-testid="new-chat-cli"
                >
                  {(pending.agent.clis ?? []).map((cli, i) => (
                    <option key={cli} value={i === 0 ? '' : cli}>
                      {cliLabel(cli)}{i === 0 ? ' (default)' : ''}
                    </option>
                  ))}
                </select>
              </label>
            )}
            <div className="flex items-center gap-2">
              <Button size="sm" disabled={creating} onClick={confirmStart}>
                Start chat
              </Button>
              {!target && (
                <button
                  type="button"
                  onClick={resetPending}
                  className="text-[11px] text-muted-foreground hover:text-foreground"
                >
                  Back
                </button>
              )}
            </div>
          </div>
        )}
      </DropdownMenuContent>
    </DropdownMenu>
  )
}


/** The agent's runner order, then the other boxes the caller may choose — each
 *  group only when it has a box, so an agent with no order reads as one list. */
function AgentRunnerOptions({ agent, runners }: { agent: string; runners: readonly AgentPinnableRunnerOut[] }) {
  const option = (r: AgentPinnableRunnerOut) => (
    <option key={r.runner_id} value={r.runner_id}>
      {r.online ? '●' : '○'} {r.runner_name}
    </option>
  )
  const ordered = runners.filter((r) => r.rank != null)
  const others = runners.filter((r) => r.rank == null)
  if (!ordered.length || !others.length) return <>{runners.map(option)}</>
  return (
    <>
      <optgroup label={`${agent}'s runner order`}>{ordered.map(option)}</optgroup>
      <optgroup label="Other runners you can use">{others.map(option)}</optgroup>
    </>
  )
}
