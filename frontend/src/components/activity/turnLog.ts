import type { components } from "@/api/generated";
import { toneClasses } from "canopy-ui/lib";

export type Turn = components["schemas"]["TurnOut"];

export type TurnFilters = {
  agent: string | null;
  origin: string | null;
  status: string | null;
};

/** Agent turns show their slug; project turns have no agent, so show the repo. */
export function agentLabel(turn: Turn): string {
  return turn.agent_slug ?? `project:${turn.project}`;
}

/** The Trigger column: what caused this turn.
 * - canopy_scheduler → the fired slot, or "run now" when a human fired it
 *   off-cycle (run_schedule_now sets origin_ref.manual and no launcher — both
 *   are scheduler work, so the distinction lives in origin_ref, not in origin)
 * - anything with a launcher → "<origin> · <who enqueued it>" (the old `manual`
 *   branch — "manual" is now just an api turn, so the launcher is the signal)
 * - otherwise → the bare origin string */
export function originLabel(turn: Turn): string {
  if (turn.origin === "canopy_scheduler") {
    const slot = turn.origin_ref?.slot;
    if (typeof slot === "string") return `canopy_scheduler · ${slot}`;
    if (turn.origin_ref?.manual) return "canopy_scheduler · run now";
    return "canopy_scheduler";
  }
  if (turn.enqueued_by_email) {
    return `${turn.origin} · ${turn.enqueued_by_email}`;
  }
  return turn.origin;
}

/** AND across the set filters; a null filter matches everything. The agent
 * filter compares against agentLabel so project turns filter by project:<name>
 * — the same string the filter dropdown offers. */
export function matchesTurnFilters(turn: Turn, filters: TurnFilters): boolean {
  if (filters.agent && agentLabel(turn) !== filters.agent) return false;
  if (filters.origin && turn.origin !== filters.origin) return false;
  if (filters.status && turn.status !== filters.status) return false;
  return true;
}

/** Status → tinted-badge className, built only from semantic tokens. Unknown
 * statuses fall back to the muted style. */
export const STATUS_TOKEN: Record<string, string> = {
  done: toneClasses("success"),
  failed: toneClasses("destructive"),
  missed: toneClasses("warning"),
  running: toneClasses("info"),
  claimed: toneClasses("neutral"),
  queued: toneClasses("neutral"),
};

export function statusToken(status: string): string {
  return STATUS_TOKEN[status] ?? toneClasses("neutral");
}
