import { useState, type JSX } from 'react'
import { Link } from 'react-router-dom'
import { pauseRunner, retireRunner, setRunnerSessions, unpauseRunner, type RunnerOut } from '@/api/harness'
import type { AgentOut } from '@/api/agents'
import { AgentRouting } from '@/components/agents/AgentRouting'
import { RunnerDrills } from '@/components/supervisor/RunnerDrills'
import { RunnerCredentials } from '@/components/supervisor/RunnerCredentials'
import { RunnerAdmins } from '@/components/supervisor/RunnerAdmins'
import { RunnerEngine } from '@/components/supervisor/RunnerEngine'
import { RunnerFlags } from '@/components/supervisor/RunnerFlags'
import { RunnerHealth } from '@/components/supervisor/RunnerHealth'

// A runner's full state — the click-through from the Runners tab's runner list.
// Leads with the signals that actually matter: is it AVAILABLE to fire a turn
// (online ∧ ready — a stale runner reporting last-known ready=true is NOT), what
// agents/repos it can drive, and who owns it (the owner that governs what it may
// work for). Below that, the fleet-wide routing matrix — editable in place, so
// "which agents route to me, and at what rank" is answerable without leaving the
// runner detail view. (Assignments are now per-RUNNER, not per-kind, so there is
// no cheap query for "agents that include just this runner" — the matrix's chips
// already surface this runner's name/rank wherever it appears.)
//
// Two hosts mount it: the supervisor's Runners tab (with Back and the routing
// matrix) and the side panel of Settings → Topology, which leaves both out —
// the map already shows who routes here, and an agent's routing is edited on
// the agent there.
export function RunnerDetail({
  runner,
  agents,
  onBack,
  onChanged,
  onRetired,
  mapHref,
  agentWorkspace,
}: {
  runner: RunnerOut
  /** Omit to leave out the per-agent routing matrix. */
  agents?: AgentOut[]
  /** Omit to leave out the Back link. */
  onBack?: () => void
  /** A pause changed this runner server-side — hand the fresh row back so the
   *  list behind this view stops disagreeing with the detail in front of it. */
  onChanged?: (runner: RunnerOut) => void
  /** Offers Retire (to whoever may manage the box) when given. */
  onRetired?: (runner: RunnerOut) => void
  /** Where this runner sits on the fleet map, when the viewer can open it. */
  mapHref?: string
  /** Which workspace an agent lives in — lets a health check link to its fix.
   *  Defaults to looking the slug up in `agents`. */
  agentWorkspace?: (slug: string) => string | undefined
}): JSX.Element {
  const online = runner.status === 'online'
  // Real availability, not last-known ready: a stale runner's ready flag is
  // whatever it reported on its final heartbeat and no longer reflects reality.
  const available = online && runner.ready
  const badge = available
    ? { text: 'available', cls: 'bg-success/15 text-success' }
    : online
      ? { text: 'not ready', cls: 'bg-destructive/15 text-destructive' }
      : { text: runner.status || 'offline', cls: 'bg-muted text-muted-foreground' }
  const caps = (runner.capabilities ?? {}) as { agents?: string[]; projects?: string[] }
  // Every agent row starts EXPANDED — opening a runner's detail should show
  // the assignment editor rows without an extra click (the routing tab is
  // gone; this is now where routing gets edited). A small fleet makes
  // "expand every row" cheap, so simplicity wins over a per-row default.
  const [expanded, setExpanded] = useState<Set<string>>(() => new Set((agents ?? []).map((a) => a.slug)))
  const toggle = (slug: string) =>
    setExpanded((prev) => {
      const next = new Set(prev)
      if (next.has(slug)) next.delete(slug)
      else next.add(slug)
      return next
    })

  // Pause/resume. `note` is only read on the way IN — resuming clears it
  // server-side, because a reason for a pause that is over is just stale text.
  const [pausing, setPausing] = useState(false)
  const [pauseErr, setPauseErr] = useState<string | null>(null)
  const [note, setNote] = useState('')

  const togglePause = () => {
    setPausing(true)
    setPauseErr(null)
    const call = runner.paused ? unpauseRunner(runner.id) : pauseRunner(runner.id, note.trim())
    call
      .then((fresh) => {
        setNote('')
        onChanged?.(fresh)
      })
      .catch((err: unknown) => {
        // Say it failed. A pause that silently didn't take is the expensive
        // failure here — you walk away believing an account stopped spending.
        setPauseErr(err instanceof Error ? err.message : 'could not change pause state')
      })
      .finally(() => setPausing(false))
  }

  // Slack/chat sessions on or off. Was settable only over the API, so a box paired
  // before pairing set it (haldimagi-mbp-cdp, 2026-10-02) silently never appeared
  // in "start a session" and nobody could see why.
  const takesSessions = Boolean((runner.capabilities as { sessions?: boolean } | null)?.sessions)
  const [savingSessions, setSavingSessions] = useState(false)
  const [sessionsErr, setSessionsErr] = useState<string | null>(null)
  const toggleSessions = () => {
    setSavingSessions(true)
    setSessionsErr(null)
    setRunnerSessions(runner, !takesSessions)
      .then((fresh) => onChanged?.(fresh))
      .catch((err: unknown) =>
        setSessionsErr(err instanceof Error ? err.message : 'could not change session setting'),
      )
      .finally(() => setSavingSessions(false))
  }

  // Retire. Two clicks, because it drops every route to this box (the server
  // deletes its assignments and /unretire does not bring them back).
  const [confirmRetire, setConfirmRetire] = useState(false)
  const [retiring, setRetiring] = useState(false)
  const [retireErr, setRetireErr] = useState<string | null>(null)
  const retire = () => {
    setRetiring(true)
    setRetireErr(null)
    retireRunner(runner.id)
      .then(() => onRetired?.(runner))
      .catch((err: unknown) => setRetireErr(err instanceof Error ? err.message : 'could not retire'))
      .finally(() => setRetiring(false))
  }

  const row = (label: string, value: string) => (
    <div className="flex items-baseline justify-between gap-3 border-b border-border py-1.5">
      <span className="text-[11px] uppercase tracking-wide text-muted-foreground">{label}</span>
      <span className="min-w-0 truncate text-[13px] text-foreground">{value}</span>
    </div>
  )

  const agentRow = (a: AgentOut) => (
    <div key={a.slug} className="rounded-md border border-border bg-background">
      <button
        type="button"
        onClick={() => toggle(a.slug)}
        className="flex w-full items-center gap-2 px-2 py-1.5 text-left"
        data-testid={`runner-priority-agent-${a.slug}`}
      >
        <span className="min-w-0 flex-1 truncate text-[13px] text-foreground">{a.name}</span>
        <span className="shrink-0 text-muted-foreground">{expanded.has(a.slug) ? '▾' : '▸'}</span>
      </button>
      {expanded.has(a.slug) && (
        <div className="px-2 pb-2">
          <AgentRouting agentSlug={a.slug} workspace={a.workspace ?? undefined} initialTurnMode={a.turn_mode} />
        </div>
      )}
    </div>
  )

  return (
    <div className="flex flex-col gap-2" data-testid={`runner-detail-${runner.name}`}>
      {(onBack || mapHref) && (
        <div className="flex items-center justify-between gap-3">
          {onBack && (
            <button type="button" onClick={onBack} className="text-[12px] text-primary" data-testid="runner-detail-back">
              ← Runners
            </button>
          )}
          {/* The configuration home: who routes here, in context, and the add-a-runner card. */}
          {mapHref && (
            <Link to={mapHref} className="ml-auto text-[12px] text-primary hover:underline" data-testid="runner-detail-map">
              On the fleet map →
            </Link>
          )}
        </div>
      )}
      <div className="flex items-center gap-2">
        <span className={`h-2 w-2 rounded-full ${online ? 'bg-success' : 'bg-muted-foreground'}`} />
        <span className="text-[15px] font-semibold text-foreground">{runner.name}</span>
        <span
          data-testid="runner-detail-ready"
          className={`ml-auto rounded px-1.5 py-0.5 text-[11px] ${badge.cls}`}
        >
          {badge.text}
        </span>
      </div>
      {online && !runner.ready && runner.ready_note && (
        <p className="text-[12px] text-destructive" data-testid="runner-detail-why">{runner.ready_note}</p>
      )}
      <div className="rounded-lg border border-border bg-card p-3">
        {row('agents', (caps.agents ?? []).join(', ') || '—')}
        {row('projects', (caps.projects ?? []).join(', ') || '—')}
        {row('kind', runner.kind ?? '')}
        {runner.kind !== 'cloud' && row('runtime', runner.engine === 'claude-desktop' ? 'Claude desktop' : 'emdash')}
        {row('owner', runner.owner_email ?? '—')}
        {/* host only matters for emdash (per-macOS-account session reuse); cloud
            runners report no host, so skip the empty row entirely. */}
        {runner.host && row('host', runner.host)}
        {/* What code this box is actually on. Only shown when it says something:
            the cloud runner is a different program and reports neither. */}
        {runner.code_version &&
          row('runner code', `${runner.code_version}${runner.code_sha ? ` (${runner.code_sha.slice(0, 12)})` : ''}`)}
        {runner.code_branch && row('branch', runner.code_branch)}
        {row('status', runner.status ?? 'unknown')}
      </div>

      {/* What the box says about its own features, and the refresh control.
          Above pause because it answers the first question on opening a box —
          "is it actually working" — which ready alone cannot. */}
      <RunnerHealth
        runner={runner}
        onChanged={onChanged}
        agentWorkspace={agentWorkspace ?? ((slug) => agents?.find((a) => a.slug === slug)?.workspace ?? undefined)}
      />

      {/* Session runtime (canopy-web#1188): emdash or the Claude desktop app. A
          laptop runner's owner flips it here instead of in a shell on the box. */}
      {(runner.can_administer || runner.can_manage) && runner.kind !== 'cloud' && onChanged && (
        <RunnerEngine runner={runner} onChange={onChanged} />
      )}

      {/* Pause — the one control this view offers on the runner itself, and the
          only way to park a box from a phone (the alternative is the local
          ~/.canopy/PAUSED sentinel, which needs a shell on that macOS account).
          Owner-only: POST /pause resolves through _runner_visibility_q, the same
          predicate can_manage reports, so rendering it for anyone else would
          hand out a button that 404s. */}
      {runner.can_manage && (
        <div className="flex flex-col gap-2 rounded-lg border border-border bg-card p-3" data-testid="runner-pause">
          <div className="flex items-center gap-2">
            <span className="text-[11px] uppercase tracking-wide text-muted-foreground">
              {runner.paused ? 'Paused' : 'Routing'}
            </span>
            <button
              type="button"
              onClick={togglePause}
              disabled={pausing}
              data-testid="runner-pause-toggle"
              className={`ml-auto rounded-md px-2.5 py-1 text-[12px] font-medium disabled:opacity-50 ${
                runner.paused
                  ? 'bg-primary text-primary-foreground'
                  : 'border border-border text-foreground hover:bg-muted'
              }`}
            >
              {pausing ? '…' : runner.paused ? 'Resume' : 'Pause'}
            </button>
          </div>
          {runner.paused ? (
            <p className="text-[12px] text-muted-foreground" data-testid="runner-pause-why">
              {runner.paused_note || 'No reason given.'}{' '}
              {runner.unpause_at ? (
                <span data-testid="runner-unpause-at">
                  Resumes on its own at {new Date(runner.unpause_at).toLocaleString()} — resume
                  now to let it claim sooner.
                </span>
              ) : (
                <>Queued work waits for this runner rather than failing — resume to let it claim again.</>
              )}
            </p>
          ) : (
            <input
              value={note}
              onChange={(e) => setNote(e.target.value)}
              placeholder="Why? (e.g. token limit on this account)"
              maxLength={200}
              data-testid="runner-pause-note"
              className="rounded-md border border-input bg-input px-2 py-1 text-[12px] text-foreground placeholder:text-muted-foreground dark:placeholder:text-foreground-secondary"
            />
          )}
          {pauseErr && <p className="text-[12px] text-destructive" data-testid="runner-pause-error">{pauseErr}</p>}
        </div>
      )}

      {runner.can_manage && onRetired && (
        <div className="flex flex-col gap-1.5 rounded-lg border border-border bg-card p-3" data-testid="runner-retire">
          <div className="flex items-center gap-2">
            <span className="text-[11px] uppercase tracking-wide text-muted-foreground">Retire</span>
            {confirmRetire ? (
              <span className="ml-auto flex gap-2">
                <button
                  type="button"
                  onClick={() => setConfirmRetire(false)}
                  disabled={retiring}
                  className="rounded-md border border-border px-2.5 py-1 text-[12px] text-foreground hover:bg-muted"
                >
                  Keep
                </button>
                <button
                  type="button"
                  onClick={retire}
                  disabled={retiring}
                  data-testid="runner-retire-confirm"
                  className="rounded-md bg-destructive px-2.5 py-1 text-[12px] font-medium text-destructive-foreground disabled:opacity-50"
                >
                  {retiring ? '…' : 'Retire it'}
                </button>
              </span>
            ) : (
              <button
                type="button"
                onClick={() => setConfirmRetire(true)}
                data-testid="runner-retire-toggle"
                className="ml-auto rounded-md border border-border px-2.5 py-1 text-[12px] text-destructive hover:bg-muted"
              >
                Retire…
              </button>
            )}
          </div>
          <p className="text-[12px] text-muted-foreground">
            {confirmRetire
              ? 'Every agent stops routing here, and un-retiring does not restore those routes. Pause instead if it is coming back.'
              : 'Decommission this box. Use Pause for a box that is coming back.'}
          </p>
          {retireErr && <p className="text-[12px] text-destructive">{retireErr}</p>}
        </div>
      )}

      {/* Takes Slack & chat sessions — same owner-only gate as Pause. Shown
          read-only to everyone else, because "why isn't this box in the session
          picker?" is a question any member asks. */}
      <div className="flex flex-col gap-1 rounded-lg border border-border bg-card p-3" data-testid="runner-sessions">
        <div className="flex items-center gap-2">
          <span className="text-[11px] uppercase tracking-wide text-muted-foreground">Slack &amp; chat sessions</span>
          {runner.can_manage ? (
            <button
              type="button"
              onClick={toggleSessions}
              disabled={savingSessions}
              data-testid="runner-sessions-toggle"
              className={`ml-auto rounded-md px-2.5 py-1 text-[12px] font-medium disabled:opacity-50 ${
                takesSessions ? 'border border-border text-foreground hover:bg-muted' : 'bg-primary text-primary-foreground'
              }`}
            >
              {savingSessions ? '…' : takesSessions ? 'Turn off' : 'Turn on'}
            </button>
          ) : (
            <span className="ml-auto text-[12px] text-foreground" data-testid="runner-sessions-state">
              {takesSessions ? 'on' : 'off'}
            </span>
          )}
        </div>
        <p className="text-[12px] text-muted-foreground">
          {takesSessions
            ? 'Takes Slack threads and web chat, and appears in "start a session".'
            : 'Off — this box never takes Slack or chat work and is hidden from "start a session".'}
        </p>
        {sessionsErr && <p className="text-[12px] text-destructive" data-testid="runner-sessions-error">{sessionsErr}</p>}
      </div>

      {/* Owner- and admin-only surface. The fleet list is workspace-scoped since
          _runner_read_q, so this view can open a runner the caller neither paired
          nor administers; rendering these panels for them would produce a 404
          rather than a control. Say whose
          box it is instead — "nothing here" is indistinguishable from a broken
          page, and naming the owner makes "ask them to declare it" a next step. */}
      {/* TWO tiers, and they gate different routes — which is the whole reason
          they are separate flags. Administering a box (its credentials, its
          browser sign-in, starting a readiness check) resolves through
          _runner_admin_or_404; speaking AS it (pause, retire, heartbeat, claim)
          stays with the owner. Gating both on can_manage meant the identity a
          box RUNS AS could not sign it back in, because someone else had run
          the pairing command. */}
      {runner.can_administer && runner.kind === 'cloud' && (
        <RunnerCredentials runnerId={runner.id} />
      )}
      {/* Who else may fix this box. Rendered for an administrator (who may read
          the list) as well as the owner (who may edit it) — the panel itself
          hides the form when there is nothing the viewer may change. */}
      {(runner.can_administer || runner.can_manage) && (
        <RunnerAdmins
          runnerId={runner.id}
          canManage={runner.can_manage}
          ownerEmail={runner.owner_email}
        />
      )}
      {runner.can_administer && onChanged && <RunnerFlags runner={runner} onChange={onChanged} />}
      {/* Readiness checks are runner-ADMIN (owner decision 2026-10-04): the
          owner, or anyone they granted administration. A non-owner admin drills
          only the agents they also administer — the server says which. */}
      {(runner.can_administer || runner.can_manage) && <RunnerDrills runnerId={runner.id} />}
      {!runner.can_manage && (
        <p className="text-[12px] text-muted-foreground" data-testid="runner-detail-readonly">
          {runner.can_administer
            ? `You can sign this box in, set its credentials, declare what it runs with (ZDR)
               and check its readiness for the agents you administer. Pausing or retiring it
               belongs to its owner, ${runner.owner_email ?? 'someone else'}.`
            : `Read-only — this runner is owned by ${runner.owner_email ?? 'someone else'},
               who can check its readiness or grant you administration; they or a runner admin
               can change what it declares.`}
        </p>
      )}

      {/* The fleet-wide routing matrix, expandable per agent — no cheap query for
          "agents that route to just this runner" now that assignments are
          per-runner rather than per-kind (see file header). */}
      {agents && (
        <div className="flex flex-col gap-1.5" data-testid="runner-priority">
          <span className="text-[11px] uppercase tracking-wide text-muted-foreground">Agent routing</span>
          {agents.length === 0 && <p className="text-[12px] text-muted-foreground">No agents.</p>}
          {agents.map((a) => agentRow(a))}
        </div>
      )}
    </div>
  )
}
