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
import { createSession } from '@/api/chat'
import { getAgentDefaultOrder, getAgentRunners, type AgentOut, type AgentRunnerOut } from '@/api/agents'
import { listRunners, type RunnerOut } from '@/api/harness'
import { projectsApi, type ProjectSlug } from '@/api/projects'
import { onlineSessionCapableRunners } from './runnerEligibility'

// The "Run on" picker's pending target — set when the user picks an agent or
// project from the menu, before they've confirmed a runner + Start.
type PendingTarget =
  | { kind: 'agent'; agent: AgentOut }
  | { kind: 'project'; project: ProjectSlug }

/**
 * "+ New chat": pick an agent (or an agentless project), pick where it runs,
 * start. CROSS-WORKSPACE — the chat is created in the chosen agent's or
 * project's own workspace. One implementation, two doors: the Sessions list
 * and the supervisor feed.
 *
 * `projects` may be passed by a caller that already loaded them; otherwise the
 * menu loads them itself.
 */
export function NewChatMenu({
  agents,
  projects: projectsProp,
  onError,
}: {
  agents: readonly AgentOut[]
  projects?: readonly ProjectSlug[]
  onError: (message: string) => void
}) {
  const navigate = useNavigate()
  const [ownProjects, setOwnProjects] = useState<ProjectSlug[]>([])
  const projects = projectsProp ?? ownProjects
  const [creating, setCreating] = useState(false)
  const [pending, setPending] = useState<PendingTarget | null>(null)
  const [agentRunnerOptions, setAgentRunnerOptions] = useState<AgentRunnerOut[]>([])
  const [fleetRunners, setFleetRunners] = useState<RunnerOut[] | null>(null)
  const [runnersLoading, setRunnersLoading] = useState(false)
  const [selectedRunnerId, setSelectedRunnerId] = useState('')

  useEffect(() => {
    if (projectsProp) return
    let live = true
    projectsApi
      .listSlugs()
      .then((p) => { if (live) setOwnProjects(p) })
      .catch(() => { /* agents alone still make a usable menu */ })
    return () => {
      live = false
    }
  }, [projectsProp])

  const start = useCallback(
    (args: Parameters<typeof createSession>[0]) => {
      setCreating(true)
      createSession(args)
        .then((s) => navigate(`/w/${s.workspace}/chat/${s.id}`))
        .catch((err: unknown) => {
          onError(err instanceof Error ? err.message : 'could not start chat')
          setCreating(false)
        })
    },
    [navigate, onError],
  )

  // Reset the "Run on" step, e.g. when the menu closes.
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
    // agent's assigned runners, so it may still list a runner that isn't
    // sessions-capable. The server is the actual gate: picking one here 422s
    // the send/place call (canopy_sessions.services._placeable_runner rejects
    // a non-session-capable runner) rather than pinning a turn no runner can
    // ever claim.
    // Its own runners, or — when it has none — the workspace default order it
    // follows (2026-10-03). Reading only its own left "Auto" as the sole choice
    // for every agent that follows a default.
    const workspace = agent.workspace ?? undefined
    getAgentDefaultOrder(agent.slug, workspace)
      .then((d) => (d.own ? getAgentRunners(agent.slug, workspace) : Array.from(d.runners ?? [])))
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
    if (pending.kind === 'agent') {
      start({ agentSlug: pending.agent.slug, workspace: pending.agent.workspace ?? undefined, runnerId })
    } else {
      start({ project: pending.project.slug, workspace: pending.project.workspace ?? undefined, runnerId })
    }
  }, [pending, selectedRunnerId, start])

  // Project chats route through the fleet-wide runner list, filtered to
  // online + sessions-capable (only those can execute a chat turn at all).
  const projectRunnerOptions = useMemo(
    () => onlineSessionCapableRunners(fleetRunners ?? []),
    [fleetRunners],
  )

  return (
    <DropdownMenu onOpenChange={(open: boolean) => { if (!open) resetPending() }}>
      <DropdownMenuTrigger
        render={
          <Button
            size="sm"
            disabled={creating || (agents.length === 0 && projects.length === 0)}
            data-testid="new-chat"
          />
        }
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
  )
}
