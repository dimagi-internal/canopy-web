import { useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { listAgentRoster, type AgentRosterRow } from '@/api/agents'
import { relativeAge } from '@/lib/relativeAge'
import { roleAllows } from '@/lib/workspaceRoles'
import { useWorkspaceRole } from '@/workspace/WorkspaceProvider'
import { scopedPath } from '../lib/scopedLinks'
import { Td, Th, Tr } from '@/components/DataTable'

// The workspace's agents, one row each: who it is and what it is doing — tasks
// open and waiting on a person, active projects, and its turns (the last one and
// how it ended, and how many ran and failed this week). A table, so the fleet
// can be scanned; each row opens the agent's own workspace.

const FAILED = new Set(['failed', 'lost'])

function Avatar({ agent }: { agent: AgentRosterRow }) {
  return agent.avatar_url ? (
    <img src={agent.avatar_url} alt="" className="h-7 w-7 shrink-0 rounded-full border border-border object-cover" />
  ) : (
    <span className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-primary/90 text-[12px] font-semibold text-primary-foreground">
      {(agent.name || agent.slug).slice(0, 1).toUpperCase()}
    </span>
  )
}

function Num({ n, warn }: { n: number; warn?: boolean }) {
  return (
    <span className={`tabular-nums ${n === 0 ? 'text-muted-foreground' : warn ? 'font-medium text-warning' : 'text-foreground'}`}>
      {n}
    </span>
  )
}

export function AgentsPage() {
  const { workspace = '' } = useParams<{ workspace: string }>()
  const canCreate = roleAllows(useWorkspaceRole(workspace), 'agent.work')
  const [agents, setAgents] = useState<AgentRosterRow[] | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    listAgentRoster()
      .then((rows) => !cancelled && setAgents(rows))
      .catch((err: unknown) => !cancelled && setError(err instanceof Error ? err.message : 'Failed to load agents'))
    return () => {
      cancelled = true
    }
  }, [workspace])

  const now = new Date()

  return (
    <div>
      <div className="mb-4 flex items-start justify-between gap-4">
        <div>
          <h1 className="text-xl font-semibold text-foreground">Agents</h1>
          <p className="mt-1 text-[13px] text-muted-foreground">
            Every agent in this workspace and what it is doing. To use one in your own Claude Code,{' '}
            <Link to="/get-started" className="text-primary hover:underline">get started</Link>.
          </p>
        </div>
        {canCreate && (
          <Link
            to={scopedPath(workspace, '/agents/new')}
            className="shrink-0 rounded-md bg-primary px-3 py-1.5 text-[13px] font-medium text-primary-foreground hover:bg-primary/90"
          >
            New agent
          </Link>
        )}
      </div>

      {error && <p className="text-sm text-destructive">{error}</p>}
      {!agents && !error && <p className="text-sm text-muted-foreground">Loading…</p>}

      {agents && agents.length > 0 && (
        <div className="overflow-x-auto rounded border border-border">
          <table className="w-full text-[13px]">
            <thead className="bg-card text-left text-[11px] uppercase tracking-wide text-muted-foreground">
              <tr>
                <Th density="roomy">Agent</Th>
                <Th density="roomy">Last turn</Th>
                <Th density="roomy" className="text-right" title="Turns in the last 7 days">Turns 7d</Th>
                <Th density="roomy" className="text-right" title="Failed or lost in the last 7 days">Failed</Th>
                <Th density="roomy" className="text-right" title="Tasks not done">Open</Th>
                <Th density="roomy" className="text-right" title="Open tasks waiting on a person">Waiting</Th>
                <Th density="roomy" className="text-right" title="Active projects">Projects</Th>
                <Th density="roomy">Mode</Th>
                <Th density="roomy">Owner</Th>
              </tr>
            </thead>
            <tbody>
              {agents.map((a) => (
                <Tr rule="top" top key={a.slug} className="hover:bg-card/60">
                  <Td density="roomy">
                    <Link to={a.url} className="flex items-start gap-2.5">
                      <Avatar agent={a} />
                      <span className="min-w-0">
                        <span className="block font-medium text-foreground hover:text-primary">{a.name}</span>
                        {a.description && <span className="line-clamp-1 max-w-md text-[12px] text-muted-foreground">{a.description}</span>}
                      </span>
                    </Link>
                  </Td>
                  <Td density="roomy" nowrap>
                    {a.last_turn_at ? (
                      <span className={FAILED.has(a.last_turn_status) ? 'text-destructive' : 'text-foreground-secondary'}
                        title={`${a.last_turn_status} · ${new Date(a.last_turn_at).toLocaleString()}`}>
                        {relativeAge(a.last_turn_at, now, { seconds: false })}
                        {FAILED.has(a.last_turn_status) && ' · failed'}
                      </span>
                    ) : (
                      <span className="text-muted-foreground">never</span>
                    )}
                  </Td>
                  <Td density="roomy" align="right"><Num n={a.turns_7d} /></Td>
                  <Td density="roomy" align="right">
                    {a.failed_7d ? <span className="tabular-nums font-medium text-destructive">{a.failed_7d}</span> : <Num n={0} />}
                  </Td>
                  <Td density="roomy" align="right"><Num n={a.open_tasks} /></Td>
                  <Td density="roomy" align="right"><Num n={a.waiting_tasks} warn /></Td>
                  <Td density="roomy" align="right"><Num n={a.active_projects} /></Td>
                  <Td density="roomy" tone="secondary">{a.turn_mode}</Td>
                  <Td density="roomy" tone="secondary">{a.owner_email ?? '—'}</Td>
                </Tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {agents && agents.length === 0 && (
        <div className="rounded-lg border border-border bg-card p-4">
          <h2 className="text-sm font-semibold text-foreground">No agents in this workspace</h2>
          <p className="mt-2 text-[13px] leading-relaxed text-foreground-secondary">
            An agent is a persona with its own git repo — skills, hooks and an identity.{' '}
            {canCreate ? (
              <>
                <Link to={scopedPath(workspace, '/agents/new')} className="text-primary hover:underline">
                  Create one
                </Link>{' '}
                here: canopy makes the repo from its agent template, registers it, and walks you
                through the rest. Or run <code className="text-primary">/canopy:create-agent</code> in
                Claude Code — same template.
              </>
            ) : (
              'Creating one needs the editor role in this workspace.'
            )}
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
