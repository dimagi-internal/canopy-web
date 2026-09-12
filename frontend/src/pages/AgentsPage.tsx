import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { listAgents, type AgentOut } from '@/api/agents'

function formatDate(s: string): string {
  return new Date(s).toLocaleDateString(undefined, {
    month: 'short',
    day: 'numeric',
    year: 'numeric',
  })
}

function CountStat({ value, label }: { value: number; label: string }) {
  return (
    <div className="flex flex-col">
      <span className="text-base font-semibold text-foreground leading-none">{value}</span>
      <span className="text-[10px] uppercase tracking-wide text-muted-foreground mt-1">{label}</span>
    </div>
  )
}

function AgentCard({ agent }: { agent: AgentCardData }) {
  return (
    <Link
      to={`/agents/${encodeURIComponent(agent.slug)}`}
      className="group block bg-card border border-border rounded-xl p-5 hover:border-primary/40 hover:bg-card/80 transition-colors"
    >
      <div className="flex items-start gap-3">
        {agent.avatar_url ? (
          <img
            src={agent.avatar_url}
            alt=""
            className="h-10 w-10 rounded-full shrink-0 object-cover border border-border"
          />
        ) : (
          <span className="h-10 w-10 rounded-full shrink-0 bg-primary/90 text-white text-sm font-semibold flex items-center justify-center">
            {(agent.name || agent.slug).slice(0, 1).toUpperCase()}
          </span>
        )}
        <div className="min-w-0 flex-1">
          <h3 className="text-[15px] font-semibold text-foreground leading-snug group-hover:text-primary transition-colors truncate">
            {agent.name}
          </h3>
          {agent.email && (
            <p className="text-[11px] text-muted-foreground truncate">{agent.email}</p>
          )}
        </div>
      </div>

      {agent.description && (
        <p className="text-[13px] text-foreground-secondary leading-relaxed mt-3 line-clamp-3">
          {agent.description}
        </p>
      )}

      <div className="flex items-end gap-5 mt-4 pt-4 border-t border-border">
        <CountStat value={agent.sync_count} label="Syncs" />
        <CountStat value={agent.work_product_count} label="Work" />
        <CountStat value={agent.skill_count} label="Skills" />
      </div>

      <p className="text-[11px] text-muted-foreground mt-3">
        {agent.latest_sync_at
          ? `Last sync ${formatDate(agent.latest_sync_at)}`
          : 'No syncs yet'}
      </p>
    </Link>
  )
}

// AgentOut from the list endpoint is the bare shape, but the workspace cards
// want the counts + latest_sync_at that only AgentDetailOut carries. The list
// endpoint returns AgentOut; counts default to 0 when absent so the same card
// renders for both shapes.
type AgentCardData = AgentOut & {
  sync_count: number
  work_product_count: number
  skill_count: number
  latest_sync_at: string | null
}

function toCardData(a: AgentOut): AgentCardData {
  const anyA = a as Partial<AgentCardData>
  return {
    ...a,
    sync_count: anyA.sync_count ?? 0,
    work_product_count: anyA.work_product_count ?? 0,
    skill_count: anyA.skill_count ?? 0,
    latest_sync_at: anyA.latest_sync_at ?? null,
  }
}

export function AgentsPage() {
  const [agents, setAgents] = useState<AgentCardData[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    void (async () => {
      try {
        const page = await listAgents({ limit: 200 })
        if (!cancelled) setAgents(page.items.map(toCardData))
      } catch (err) {
        if (!cancelled) setError(err instanceof Error ? err.message : 'Failed to load agents')
      } finally {
        if (!cancelled) setLoading(false)
      }
    })()
    return () => {
      cancelled = true
    }
  }, [])

  return (
    <div>
      <div className="mb-6">
        <h1 className="text-xl font-semibold text-foreground">Agents</h1>
        <p className="text-[13px] text-muted-foreground mt-1">
          Each agent's workspace — its syncs, work products, and skill catalog. Pick one to open
          its workspace.
        </p>
      </div>

      {error && (
        <div className="flex items-center justify-center h-48 text-destructive text-sm">{error}</div>
      )}

      {loading && (
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
          {Array.from({ length: 6 }).map((_, i) => (
            <div
              key={i}
              className="bg-card border border-border rounded-xl p-5 animate-pulse"
              style={{ animationDelay: `${i * 80}ms` }}
            >
              <div className="flex items-center gap-3 mb-4">
                <div className="h-10 w-10 rounded-full bg-muted" />
                <div className="flex-1 space-y-2">
                  <div className="h-4 bg-muted rounded w-2/3" />
                  <div className="h-3 bg-muted/70 rounded w-1/2" />
                </div>
              </div>
              <div className="h-3 bg-muted/70 rounded w-full mb-2" />
              <div className="h-3 bg-muted/70 rounded w-4/5" />
            </div>
          ))}
        </div>
      )}

      {!loading && !error && agents.length > 0 && (
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
          {agents.map((a) => (
            <AgentCard key={a.id} agent={a} />
          ))}
        </div>
      )}

      {!loading && !error && agents.length === 0 && (
        <div className="rounded-lg border border-border bg-card p-4">
          <h2 className="text-sm font-semibold text-foreground">No agents in this workspace</h2>
          <p className="mt-2 text-[13px] leading-relaxed text-foreground-secondary">
            An agent is a persona with its own git repo — skills, hooks and an identity. It is
            not created here: run <code className="text-primary">/canopy:create-agent</code> in
            Claude Code, which scaffolds the repo, then register it in this workspace for it
            to appear in this list. That command needs the canopy plugin installed, which
            needs a Personal Access Token — mint one from{' '}
            <Link to="/settings" className="text-primary hover:underline">Settings</Link>.
          </p>
          <p className="mt-2 text-[12px] text-muted-foreground">
            You will also need a runner online to execute its turns.{' '}
            <Link to="/guide#/w/:workspace/agents" className="text-primary hover:underline">
              Read more in the guide
            </Link>
            .
          </p>
        </div>
      )}
    </div>
  )
}
