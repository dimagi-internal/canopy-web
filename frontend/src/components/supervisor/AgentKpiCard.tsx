import { useState, type JSX, type MouseEvent } from 'react'
import { Link } from 'react-router-dom'
import { setAgentTurnMode, type AgentOut, type TurnMode } from '@/api/agents'

// The turn-mode chip — readable at a glance, tappable to flip. This is the
// phone-reachable control: the installed PWA opens at /supervisor, so the
// Agents tab is the home screen and flipping a mode shouldn't cost four taps
// into the agent's own page.
//
// It lives INSIDE the card's <Link>, so every handler preventDefault +
// stopPropagation — a tap on the chip must never also navigate.
//
// Asymmetric by design. gated -> auto hands an agent permission to send without
// asking, so it takes a confirm (a fat-fingered tap on a phone must not put an
// agent on the loose). auto -> gated only ever REMOVES permission, so it applies
// on first tap: never make the safe direction harder than the dangerous one.
function TurnModeChip({ slug, mode }: { slug: string; mode: TurnMode }): JSX.Element {
  const [current, setCurrent] = useState<TurnMode>(mode)
  const [busy, setBusy] = useState(false)
  const [failed, setFailed] = useState(false)

  const onTap = async (e: MouseEvent<HTMLButtonElement>) => {
    e.preventDefault()
    e.stopPropagation()
    if (busy) return
    const next: TurnMode = current === 'auto' ? 'gated' : 'auto'
    if (
      next === 'auto' &&
      !window.confirm(
        `Put ${slug} in auto mode? It will reply and take outbound actions without waiting for your approval.`,
      )
    ) {
      return
    }
    setBusy(true)
    setFailed(false)
    const prev = current
    setCurrent(next)
    try {
      await setAgentTurnMode(slug, next)
    } catch {
      // Put the chip back where it was: a silent revert would read as "it
      // worked" on a phone with no console open.
      setCurrent(prev)
      setFailed(true)
    } finally {
      setBusy(false)
    }
  }

  const auto = current === 'auto'
  return (
    <button
      type="button"
      onClick={(e) => void onTap(e)}
      disabled={busy}
      data-testid={`agent-mode-${slug}`}
      aria-label={`${slug} turn mode: ${current}. Activate to switch to ${auto ? 'gated' : 'auto'}.`}
      title={
        auto
          ? 'Auto — sends without waiting for you. Tap to require approval again.'
          : 'Gated — outbound waits for your approval. Tap to allow unattended sending.'
      }
      className={`shrink-0 rounded border px-1.5 py-0.5 text-[9px] font-bold uppercase tracking-[0.06em] transition-colors disabled:opacity-50 ${
        auto
          ? 'border-warning/40 bg-warning/10 text-warning'
          : 'border-border bg-muted/40 text-muted-foreground'
      }`}
    >
      {failed ? 'Retry' : auto ? 'Auto' : 'Gated'}
    </button>
  )
}

// One agent's KPIs — the React counterpart of menubar.py's _card (menubar.py:385).
//
// The fleet legitimately spans workspaces (e.g. a chief-of-staff agent lives in a
// different tenant than the product agents), so the correct deep link is
// /w/<agent's workspace>/agents/<slug>. Linking to the ACTIVE workspace 404s any
// agent living elsewhere (the bug fixed once already in menubar.py, commit
// 483c821). AgentOut now serializes `workspace` (the slug), so we can build the
// tenant-scoped link directly; only a pre-tenancy agent (workspace null) falls
// back to the flat /agents/<slug> route.
export function AgentKpiCard({ agent, waiting }: { agent: AgentOut; waiting: number }): JSX.Element {
  const href = agent.workspace ? `/w/${agent.workspace}/agents/${agent.slug}` : `/agents/${agent.slug}`
  return (
    <Link
      to={href}
      data-testid={`agent-card-${agent.slug}`}
      className="flex items-center gap-3 rounded-lg border border-border bg-card p-3 transition-colors hover:border-primary/40"
    >
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-1.5">
          <p className="truncate text-[13px] font-semibold text-foreground">{agent.name}</p>
          <TurnModeChip slug={agent.slug} mode={agent.turn_mode} />
        </div>
        <p className="mt-0.5 text-[11px] text-muted-foreground">
          {waiting > 0 ? `${waiting} waiting on you` : 'nothing waiting'}
        </p>
      </div>
      {waiting > 0 && (
        <span className="shrink-0 rounded border border-primary/30 bg-primary/10 px-1.5 py-0.5 text-[11px] font-medium text-primary">
          {waiting}
        </span>
      )}
    </Link>
  )
}
