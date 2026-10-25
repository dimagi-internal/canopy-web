import { useState, type JSX } from 'react'
import { retireRunner } from '@/api/harness'
import { AgentRouting } from '@/components/agents/AgentRouting'
import { Section } from '@/components/objectPage/settingsLayout'
import { RunnerAdmins } from '@/components/supervisor/RunnerAdmins'
import { boxLoginAccount, RunnerCredentials } from '@/components/supervisor/RunnerCredentials'
import { RunnerDirected } from '@/components/supervisor/RunnerDirected'
import { RunnerEgress } from '@/components/supervisor/RunnerEgress'
import { RunnerEngine } from '@/components/supervisor/RunnerEngine'
import { RunnerFlags } from '@/components/supervisor/RunnerFlags'
import { RunnerOnlyAgents } from '@/components/supervisor/RunnerOnlyAgents'
import { RunnerPause } from '@/components/supervisor/RunnerPause'
import { RunnerPrivate } from '@/components/supervisor/RunnerPrivate'
import { RunnerProjects } from '@/components/supervisor/RunnerProjects'
import { RunnerSessions } from '@/components/supervisor/RunnerSessions'
import { RunnerWorkspaces } from '@/components/supervisor/RunnerWorkspaces'
import type { RunnerOutletContext } from './RunnerPage'

// The runner page's tabs after Overview (RunnerOverview.tsx). Each control is the
// component the supervisor used to stack in one column; who may use each is still
// the server's rule for its route, and a control the viewer cannot use is not
// offered (its route would 404).

/** Its Claude logins, tokens and API keys, in the order it uses them, and the vault
 *  they are kept in. Only for a cloud box the viewer administers. */
export function RunnerCredentialsTab({ runner }: RunnerOutletContext): JSX.Element | null {
  if (!(runner.can_administer && runner.kind === 'cloud')) return null
  const login = runner.health_checks?.['claude.login']
  return (
    <RunnerCredentials
      runnerId={runner.id}
      workspace={runner.workspace}
      signedIn={login ? login.status === 'ok' : null}
      signedInAs={boxLoginAccount(login?.detail)}
      healthChecks={runner.health_checks}
      apiKeysOnly={!!(runner.capabilities as { data_mode?: string } | null)?.data_mode}
    />
  )
}

/** Which workspaces it serves, which agents it will take, and who routes here. */
export function RunnerRoutingTab({ runner, agents, onChanged }: RunnerOutletContext): JSX.Element {
  const [open, setOpen] = useState<Set<string>>(() => new Set())
  const toggle = (slug: string) =>
    setOpen((prev) => {
      const next = new Set(prev)
      if (next.has(slug)) next.delete(slug)
      else next.add(slug)
      return next
    })
  const admin = runner.can_administer || runner.can_manage
  return (
    <div>
      {admin && (
        <Section id="workspaces" title="Workspaces it serves">
          <RunnerWorkspaces runnerId={runner.id} />
        </Section>
      )}
      {runner.can_administer && (
        <Section id="takes" title="What it takes">
          <div className="flex flex-col divide-y divide-border rounded-lg border border-border bg-card px-3">
            <RunnerOnlyAgents runner={runner} onChange={onChanged} />
            <RunnerDirected runner={runner} onChange={onChanged} />
          </div>
        </Section>
      )}
      {runner.kind === 'emdash' && (
        <Section id="projects" title="Repos (emdash projects)">
          <RunnerProjects runnerId={runner.id} agents={agents} />
        </Section>
      )}
      {/* Per agent, expandable: there is no cheap query for "agents that route to
          just this box" now that assignments are per-runner, so every agent the
          viewer can see is listed and its own routing edited in place. */}
      <Section id="agent-routing" title="Agent routing">
        {agents === null && <div className="h-6 w-48 animate-pulse rounded-md bg-muted" />}
        {agents?.length === 0 && <p className="text-[12px] text-muted-foreground">No agents.</p>}
        <div className="flex flex-col gap-1.5" data-testid="runner-priority">
          {agents?.map((a) => (
            <div key={a.slug} className="rounded-md border border-border bg-card">
              <button type="button" onClick={() => toggle(a.slug)} data-testid={`runner-priority-agent-${a.slug}`}
                className="flex w-full items-center gap-2 px-2 py-1.5 text-left">
                <span className="min-w-0 flex-1 truncate text-[13px] text-foreground">{a.name}</span>
                <span className="shrink-0 text-muted-foreground">{open.has(a.slug) ? '▾' : '▸'}</span>
              </button>
              {open.has(a.slug) && (
                <div className="px-2 pb-2">
                  <AgentRouting agentSlug={a.slug} workspace={a.workspace ?? undefined} initialTurnMode={a.turn_mode} />
                </div>
              )}
            </div>
          ))}
        </div>
      </Section>
    </div>
  )
}

/** Who else may fix this box, and whether it is its owner's alone. */
export function RunnerAccessTab({ runner, onChanged }: RunnerOutletContext): JSX.Element {
  return (
    <div>
      {(runner.can_administer || runner.can_manage) && (
        <Section id="admins" title="Administrators">
          <RunnerAdmins runnerId={runner.id} workspace={runner.workspace} canManage={runner.can_manage}
            ownerEmail={runner.owner_email} />
        </Section>
      )}
      {runner.can_manage && (
        <Section id="private" title="Private">
          <div className="rounded-lg border border-border bg-card px-3">
            <RunnerPrivate runner={runner} onChange={onChanged} />
          </div>
        </Section>
      )}
      {!runner.can_administer && !runner.can_manage && (
        <p className="text-[12px] text-muted-foreground" data-testid="runner-access-readonly">
          Its owner, {runner.owner_email ?? 'someone else'}, decides who else administers it.
        </p>
      )}
    </div>
  )
}

/** Every plain choice about the box, one row each, then Retire. */
export function RunnerSettingsTab({ runner, onChanged, onRetired }: RunnerOutletContext): JSX.Element {
  return (
    <div>
      <Section id="settings" title="Settings">
        <div className="flex flex-col divide-y divide-border rounded-lg border border-border bg-card px-3" data-testid="runner-settings">
          {runner.can_manage && <RunnerPause runner={runner} onChanged={onChanged} />}
          <RunnerSessions runner={runner} onChanged={onChanged} />
          {runner.can_administer && <RunnerFlags runner={runner} onChange={onChanged} />}
          {runner.can_administer && runner.kind === 'cloud' && <RunnerEgress runner={runner} onChange={onChanged} />}
          {(runner.can_administer || runner.can_manage) && runner.kind !== 'cloud' && (
            <RunnerEngine runner={runner} onChange={onChanged} />
          )}
        </div>
      </Section>
      {/* Last: the one setting that cannot be undone in full. */}
      {runner.can_manage && (
        <Section id="retire" title="Retire">
          <RunnerRetire runnerId={runner.id} onRetired={onRetired} />
        </Section>
      )}
    </div>
  )
}

/** Two clicks, because it drops every route to this box (the server deletes its
 *  assignments and un-retiring does not bring them back). */
function RunnerRetire({ runnerId, onRetired }: { runnerId: string; onRetired: () => void }): JSX.Element {
  const [confirm, setConfirm] = useState(false)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const retire = () => {
    setBusy(true)
    setErr(null)
    retireRunner(runnerId)
      .then(onRetired)
      .catch((e: unknown) => setErr(e instanceof Error ? e.message : 'could not retire'))
      .finally(() => setBusy(false))
  }
  return (
    <div className="flex flex-col gap-1.5 rounded-lg border border-border bg-card p-3" data-testid="runner-retire">
      <div className="flex items-center gap-2">
        <p className="text-[12px] text-muted-foreground">
          {confirm
            ? 'Every agent stops routing here, and un-retiring does not restore those routes. Pause instead if it is coming back.'
            : 'Decommission this box. Use Pause for a box that is coming back.'}
        </p>
        {confirm ? (
          <span className="ml-auto flex shrink-0 gap-2">
            <button type="button" onClick={() => setConfirm(false)} disabled={busy}
              className="rounded-md border border-border px-2.5 py-1 text-[12px] text-foreground hover:bg-muted">Keep</button>
            <button type="button" onClick={retire} disabled={busy} data-testid="runner-retire-confirm"
              className="rounded-md bg-destructive px-2.5 py-1 text-[12px] font-medium text-destructive-foreground disabled:opacity-50">
              {busy ? '…' : 'Retire it'}
            </button>
          </span>
        ) : (
          <button type="button" onClick={() => setConfirm(true)} data-testid="runner-retire-toggle"
            className="ml-auto shrink-0 rounded-md border border-border px-2.5 py-1 text-[12px] text-destructive hover:bg-muted">
            Retire…
          </button>
        )}
      </div>
      {err && <p className="text-[12px] text-destructive">{err}</p>}
    </div>
  )
}
