// Pure helpers for "which runners can execute a chat turn" — shared by the
// new-chat "Run on" picker (ChatSessionsPanel) and the offline-runner
// placement banner (ChatPage). Kept pure so both surfaces stay unit-testable
// without mounting React.
import type { RunnerOut } from "@/api/harness";

/**
 * Only a session-CAPABLE runner (capabilities.sessions === true) can execute
 * a chat turn — a runner without that capability would just leave the turn
 * QUEUED forever if picked. `RunnerOut.capabilities` is a caller-reported
 * bag of unknowns, so this checks it defensively rather than trusting the
 * shape.
 */
export function isSessionCapable(runner: Pick<RunnerOut, "capabilities">): boolean {
  return runner.capabilities?.sessions === true;
}

/**
 * A directed-only box (`directed`, a runner administrator's setting — a test box
 * pairs with it on — or a PRIVATE runner, its owner's own machine) takes only
 * what is put on it: turns pinned to it and chats bound to it — never routed
 * work. Mirrors the server's `Runner.directed_only`. It is still session-capable,
 * so it is offered for a chat (a chat started on it binds to it), labelled so
 * nobody mistakes it for a box that shares load.
 */
export function isDirectedOnly(runner: { private?: boolean; directed?: boolean }): boolean {
  return runner.private === true || runner.directed === true;
}

/** A runner flag as a person reads it: `sensitive_data` → "sensitive-data". */
export function runnerFlagLabel(flag: string): string {
  return flag.replace(/_/g, "-");
}

/** A runner's name as a picker offers it, marked when it is directed-only. */
export function runnerOptionLabel(
  runner: Pick<RunnerOut, "name"> & { private?: boolean; directed?: boolean },
): string {
  return isDirectedOnly(runner) ? `${runner.name} · directed only` : runner.name;
}

/**
 * Whether a runner declares every flag a conversation requires (e.g. `sensitive_data`,
 * from `SessionOut.runner_requirements`). The server refuses a placement that
 * fails this, so offering one would only produce an error.
 */
export function satisfiesRequirements(
  runner: { flags?: readonly string[] | null },
  requires: readonly string[] | null | undefined,
): boolean {
  const flags = runner.flags ?? [];
  return (requires ?? []).every((f) => flags.includes(f));
}

/** Online + session-capable runners from the fleet — the eligible set for
 * directed placement (new-chat "Run on" for a project chat, and the offline
 * banner's "Continue on…") — narrowed to those satisfying the conversation's
 * runner requirements when it has any. */
export function onlineSessionCapableRunners(
  fleet: readonly RunnerOut[],
  requires: readonly string[] | null | undefined = [],
): RunnerOut[] {
  return fleet.filter(
    (r) => r.status === "online" && isSessionCapable(r) && satisfiesRequirements(r, requires),
  );
}

/**
 * A repo an agentless (project) chat can be started on. `workspace` is the home
 * workspace of the first runner reporting it — the tenant the chat is created
 * in — or null to let the server use the caller's default.
 */
export type RepoChoice = { slug: string; workspace: string | null }

/** The repos a runner reports holding (`capabilities.projects` — the emdash
 * project names it can open a worktree in). Defensive: the bag is caller-reported. */
export function reportedRepos(runner: Pick<RunnerOut, "capabilities">): string[] {
  const repos = (runner.capabilities as { projects?: unknown } | null)?.projects;
  return Array.isArray(repos) ? repos.filter((r): r is string => typeof r === "string" && r !== "") : [];
}

/**
 * The repos a project chat can actually run on: the union of what the online,
 * session-capable runners report, sorted by name. This is the same list the
 * server claims against (`claim_next_turn` matches a project turn's repo to a
 * runner's `capabilities.projects`), so a repo offered here is one some box can
 * take. It replaced the retired workbench project registry, which listed repos
 * whether or not any runner held them.
 */
export function repoChoices(fleet: readonly RunnerOut[]): RepoChoice[] {
  const by = new Map<string, RepoChoice>();
  for (const r of onlineSessionCapableRunners(fleet)) {
    for (const slug of reportedRepos(r)) {
      if (!by.has(slug)) by.set(slug, { slug, workspace: r.workspace ?? null });
    }
  }
  return [...by.values()].sort((a, b) => a.slug.localeCompare(b.slug));
}

/**
 * The liveness half of a session payload — the two fields `_out` computes from
 * the binding's runner. Structural rather than the whole SessionOut so these
 * helpers stay testable from a two-key literal.
 */
export type SessionRunnerLiveness = {
  runner_online?: boolean | null
  runner_status?: string | null
  /** Whether an agent is blocked on a human here. Read only by `parkedSummary`,
   *  so a caller with no such field still type-checks. */
  waiting_on_you?: boolean | null
}

/**
 * Why a session's runner cannot take a message, or null when it can.
 *
 * `paused` and `offline` read the same to the server (both are "not ONLINE", so
 * `claim_next_turn` refuses either) but mean opposite things to a human: a pause
 * is a decision someone made and can undo from the Runners tab, while offline is
 * a box to go look at. Naming only the bool would collapse them.
 */
export function parkedReason(session: SessionRunnerLiveness): 'paused' | 'offline' | null {
  if (session.runner_online !== false) return null   // null/undefined = unbound, nothing to be offline
  return session.runner_status === 'paused' ? 'paused' : 'offline'
}

/**
 * Split a session list into the ones whose runner can act and the ones parked
 * behind a paused/offline runner. UNBOUND sessions count as live: a web chat
 * that has never sent has no runner yet and gets one when it does — hiding it
 * would make a brand-new chat vanish the moment you created it.
 */
export function partitionByRunnerReachability<T extends SessionRunnerLiveness>(
  sessions: readonly T[],
): { live: T[]; parked: T[] } {
  const live: T[] = []
  const parked: T[] = []
  for (const s of sessions) (parkedReason(s) ? parked : live).push(s)
  return { live, parked }
}

/** One line explaining what the default list is holding back — named by reason
 * when they agree, generic when a pause and a dead box are both in there.
 *
 * Names the WAITING ones specifically, because the "N waiting on you" count in
 * the heading is deliberately taken over the whole list while this filter hides
 * part of it. Without saying so, the panel reports somebody is waiting and then
 * shows nothing waiting — a dead end with no next move, reported on 2026-08-01
 * ("my phone knows something is pending but doesn't show any one session as
 * pending"). The count stays whole-list; this is what makes it followable. */
export function parkedSummary(parked: readonly SessionRunnerLiveness[]): string {
  if (parked.length === 0) return ''
  const reasons = new Set(parked.map(parkedReason))
  const why = reasons.size === 1 ? `runner ${[...reasons][0]}` : 'runner unavailable'
  const waiting = parked.filter((s) => s.waiting_on_you).length
  const tail = waiting > 0 ? ` · ${waiting} waiting on you` : ''
  return `${parked.length} hidden — ${why}${tail}`
}

/**
 * The fleet row for a session's bound runner, or null.
 *
 * The session payload carries the runner's NAME but not its id, and acting on a
 * runner (resume) needs the id — so the name is matched against the fleet list.
 * Null when the fleet hasn't
 * loaded, when the name is absent, or when the runner isn't in the caller's
 * fleet at all (retired, or owned by someone else) — every one of which means
 * "you cannot act on it from here", which is what the caller does with this.
 */
export function findBoundRunner<T extends { name: string }>(
  runnerName: string | null | undefined,
  fleet: readonly T[],
): T | null {
  if (!runnerName) return null
  return fleet.find((r) => r.name === runnerName) ?? null
}
