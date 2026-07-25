import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
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
import { createSession, listSessions, type ChatSession } from '@/api/chat'
import { getAgentRunners, listAgents, type AgentOut, type AgentRunnerOut } from '@/api/agents'
import { listRunners, type RunnerOut } from '@/api/harness'
import { projectsApi, type ProjectSlug } from '@/api/projects'
import { relativeTime } from '@/components/activity/turnLog'
import { sessionTargetLabel } from './sessionTargetLabel'
import { onlineSessionCapableRunners } from './runnerEligibility'

// The "Run on" picker's pending target — set when the user picks an agent or
// project from the New chat menu, before they've confirmed a runner + Start.
type PendingTarget =
  | { kind: 'agent'; agent: AgentOut }
  | { kind: 'project'; project: ProjectSlug }

/**
 * Reusable, CROSS-WORKSPACE chat session surface: a findable list of your chat
 * sessions (continue any from any device) + "New chat with <agent>". Each session
 * links to ITS OWN workspace's chat route, and a new chat is created in the chosen
 * agent's workspace — the fleet spans workspaces. Used by the standalone chat home
 * (/w/:ws/chat) and by the root-scoped supervisor Sessions tab, which embeds this
 * as its single unified session list (no separate grouped-by-project view).
 */
export function ChatSessionsPanel({
  agents: agentsProp,
  heading = 'Chats',
}: {
  agents?: AgentOut[]
  heading?: string
}) {
  const navigate = useNavigate()
  const [sessions, setSessions] = useState<ChatSession[]>([])
  const [agents, setAgents] = useState<AgentOut[]>(agentsProp ?? [])
  const [projects, setProjects] = useState<ProjectSlug[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [creating, setCreating] = useState(false)

  // "Run on" picker state: a target (agent or project) picked from the New
  // chat menu, its eligible runners, and the current selection ('' = Auto).
  const [pending, setPending] = useState<PendingTarget | null>(null)
  const [agentRunnerOptions, setAgentRunnerOptions] = useState<AgentRunnerOut[]>([])
  const [fleetRunners, setFleetRunners] = useState<RunnerOut[] | null>(null)
  const [runnersLoading, setRunnersLoading] = useState(false)
  const [selectedRunnerId, setSelectedRunnerId] = useState('')

  useEffect(() => {
    if (agentsProp) setAgents(agentsProp)
  }, [agentsProp])

  useEffect(() => {
    let live = true
    setLoading(true)
    // Sessions + projects always load (projects feed the "+ New chat" dropdown);
    // agents load unless provided by a prop.
    const jobs: Promise<unknown>[] = [listSessions(), projectsApi.listSlugs()]
    if (!agentsProp) jobs.push(listAgents({ limit: 100 }))
    Promise.allSettled(jobs).then((results) => {
      if (!live) return
      const [s, p, a] = results
      if (s.status === 'fulfilled') setSessions(s.value as ChatSession[])
      else setError(s.reason instanceof Error ? s.reason.message : 'failed to load sessions')
      if (p.status === 'fulfilled') setProjects(p.value as ProjectSlug[])
      if (!agentsProp && a && a.status === 'fulfilled') {
        setAgents((a.value as { items: AgentOut[] }).items)
      }
      setLoading(false)
    })
    return () => {
      live = false
    }
  }, [agentsProp])

  // A slow REST refresh keeps the unified list current (the live push into the
  // list is a deferred follow-up; per-row liveness is live inside ChatPanel).
  useEffect(() => {
    const id = window.setInterval(() => {
      listSessions()
        .then(setSessions)
        .catch(() => { /* keep last-good; the mount fetch owns first-error surfacing */ })
    }, 20_000)
    return () => window.clearInterval(id)
  }, [])

  const agentName = useMemo(() => {
    const by = new Map(agents.map((a) => [a.slug, a.name]))
    return (slug: string | null) => (slug ? by.get(slug) ?? slug : null)
  }, [agents])

  const startChat = useCallback(
    (agent: AgentOut, runnerId?: string) => {
      setCreating(true)
      createSession({ agentSlug: agent.slug, workspace: agent.workspace ?? undefined, runnerId })
        .then((s) => navigate(`/w/${s.workspace}/chat/${s.id}`))
        .catch((err: unknown) => {
          setError(err instanceof Error ? err.message : 'could not start chat')
          setCreating(false)
        })
    },
    [navigate],
  )

  const startProjectChat = useCallback(
    (project: ProjectSlug, runnerId?: string) => {
      setCreating(true)
      createSession({ project: project.slug, workspace: project.workspace ?? undefined, runnerId })
        .then((s) => navigate(`/w/${s.workspace}/chat/${s.id}`))
        .catch((err: unknown) => {
          setError(err instanceof Error ? err.message : 'could not start chat')
          setCreating(false)
        })
    },
    [navigate],
  )

  // Reset the "Run on" step, e.g. when the New chat menu closes.
  const resetPending = useCallback(() => {
    setPending(null)
    setSelectedRunnerId('')
  }, [])

  // pickAgent is an event handler, not an effect, so there's no cleanup
  // function to cancel a stale in-flight fetch — a ref tracking the
  // currently-picked slug lets a late response check whether it's still
  // wanted before applying, so rapidly switching agents can't have a slower
  // earlier response overwrite a newer pick's runner options.
  const pickedAgentSlugRef = useRef<string | null>(null)

  const pickAgent = useCallback((agent: AgentOut) => {
    setSelectedRunnerId('')
    setPending({ kind: 'agent', agent })
    setRunnersLoading(true)
    pickedAgentSlugRef.current = agent.slug
    // AgentRunnerOut does NOT carry `capabilities` — this list is just the
    // agent's assigned runners. If the picked runner isn't sessions-capable,
    // the server routing safely leaves the turn QUEUED rather than dropping it.
    getAgentRunners(agent.slug)
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

  const pickProject = useCallback(
    (project: ProjectSlug) => {
      setSelectedRunnerId('')
      setPending({ kind: 'project', project })
      if (fleetRunners !== null) return
      setRunnersLoading(true)
      listRunners()
        .then(setFleetRunners)
        .catch(() => setFleetRunners([]))
        .finally(() => setRunnersLoading(false))
    },
    [fleetRunners],
  )

  const confirmStart = useCallback(() => {
    if (!pending) return
    const runnerId = selectedRunnerId || undefined
    if (pending.kind === 'agent') startChat(pending.agent, runnerId)
    else startProjectChat(pending.project, runnerId)
  }, [pending, selectedRunnerId, startChat, startProjectChat])

  // Project chats route through the fleet-wide runner list, filtered to
  // online + sessions-capable (only those can execute a chat turn at all).
  const projectRunnerOptions = useMemo(
    () => onlineSessionCapableRunners(fleetRunners ?? []),
    [fleetRunners],
  )

  const now = new Date()

  return (
    <div className="flex min-h-0 flex-col">
      <div className="flex items-center justify-between gap-2 pb-2">
        <h2 className="text-sm font-semibold text-foreground">{heading}</h2>
        <DropdownMenu onOpenChange={(open: boolean) => { if (!open) resetPending() }}>
          <DropdownMenuTrigger
            render={<Button size="sm" disabled={creating || (agents.length === 0 && projects.length === 0)} />}
          >
            <Plus className="mr-1 h-4 w-4" />
            New chat
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
                    <DropdownMenuLabel>Projects</DropdownMenuLabel>
                    {projects.map((p) => (
                      <DropdownMenuItem
                        key={`${p.workspace}/${p.slug}`}
                        closeOnClick={false}
                        onClick={() => pickProject(p)}
                      >
                        {p.name}
                        {p.workspace ? <span className="ml-2 text-xs text-muted-foreground">{p.workspace}</span> : null}
                      </DropdownMenuItem>
                    ))}
                  </>
                )}
              </>
            ) : (
              <div className="flex flex-col gap-2 px-2 py-1.5" data-testid="run-on-picker">
                <div className="text-sm text-foreground">
                  {pending.kind === 'agent' ? pending.agent.name : pending.project.name}
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
                      ? agentRunnerOptions.map((r) => (
                          <option key={r.runner_id} value={r.runner_id}>
                            {r.online ? '●' : '○'} {r.runner_name}
                          </option>
                        ))
                      : projectRunnerOptions.map((r) => (
                          <option key={r.id} value={r.id}>
                            ● {r.name}
                          </option>
                        ))}
                  </select>
                </label>
                <div className="flex items-center gap-2">
                  <Button size="sm" disabled={creating} onClick={confirmStart}>
                    Start chat
                  </Button>
                  <button
                    type="button"
                    onClick={resetPending}
                    className="text-[11px] text-muted-foreground hover:text-foreground"
                  >
                    Back
                  </button>
                </div>
              </div>
            )}
          </DropdownMenuContent>
        </DropdownMenu>
      </div>

      {error && <div className="py-2 text-sm text-destructive">{error}</div>}
      {loading ? (
        <div className="py-6 text-sm text-muted-foreground">Loading sessions…</div>
      ) : sessions.length === 0 ? (
        <div className="flex flex-col items-center justify-center gap-1 py-12 text-center">
          <div className="text-sm text-foreground">No chats yet</div>
          <div className="text-xs text-muted-foreground">Start one with “New chat”.</div>
        </div>
      ) : (
        <ul className="divide-y divide-border rounded-md border border-border">
          {sessions.map((s) => {
            const label = sessionTargetLabel(agentName(s.agent_slug), s.project ?? '')
            return (
              <li key={s.id}>
                <Link
                  to={`/w/${s.workspace}/chat/${s.id}`}
                  className="flex items-center justify-between gap-3 px-3 py-2.5 hover:bg-muted"
                >
                  <div className="min-w-0">
                    <div className="truncate text-sm font-medium text-foreground">
                      {s.title?.trim() || 'Untitled chat'}
                    </div>
                    <div className="truncate text-xs text-muted-foreground">
                      {label} · {s.workspace}
                      {s.origin === 'runner' ? ' · discovered' : ''}
                      {s.status !== 'active' ? ` · ${s.status}` : ''}
                    </div>
                  </div>
                  <div className="flex shrink-0 flex-col items-end gap-0.5 text-xs">
                    {s.running ? (
                      <span className="flex items-center gap-1 font-medium text-success">
                        <span className="inline-block h-1.5 w-1.5 animate-pulse rounded-full bg-success" />
                        running
                      </span>
                    ) : (
                      <span className="text-muted-foreground">{relativeTime(s.created_at, now)}</span>
                    )}
                    {s.runner_name && (
                      <span className="text-muted-foreground">
                        {s.runner_name}
                        {s.runner_location ? ` · ${s.runner_location}` : ''}
                      </span>
                    )}
                  </div>
                </Link>
              </li>
            )
          })}
        </ul>
      )}
    </div>
  )
}

export default ChatSessionsPanel
