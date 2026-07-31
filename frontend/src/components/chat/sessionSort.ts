/**
 * Display ordering for the unified Sessions list.
 *
 * Sorts on `last_activity_at` — when the session last DID something — NOT
 * `created_at`, which for a runner-discovered session is when the report sweep
 * first noticed it and is therefore identical across every session in that
 * sweep (a dead repo and a live one both read "4h ago").
 */
export type SessionSort = "time" | "project";

type Sortable = {
  project?: string | null;
  running?: boolean;
  waiting_on_you?: boolean;
  last_activity_at: string;
};

const activityDesc = (a: Sortable, b: Sortable) =>
  Date.parse(b.last_activity_at) - Date.parse(a.last_activity_at);

/** A session blocked on a human sorts above everything, in every mode.
 *
 *  Activity ordering actively buries it: a session stops writing the instant it
 *  asks, so the longer somebody has been kept waiting the further down it goes
 *  — the one row you can act on ends up under a dozen you cannot. */
const waitingFirst = (a: Sortable, b: Sortable) =>
  Number(Boolean(b.waiting_on_you)) - Number(Boolean(a.waiting_on_you));

/** Sort a copy; never mutates the input. */
export function sortSessions<T extends Sortable>(
  rows: readonly T[],
  mode: SessionSort,
): T[] {
  const copy = [...rows];
  if (mode === "project") {
    // Grouped by repo, and inside a repo: waiting on a human, then running,
    // then most-recently-active. Blank projects (web chats) sort last, not
    // first. Waiting stays INSIDE the group here — project mode's whole
    // contract is that a row appears under its repo heading.
    return copy.sort(
      (a, b) =>
        projectKey(a).localeCompare(projectKey(b)) ||
        waitingFirst(a, b) ||
        Number(Boolean(b.running)) - Number(Boolean(a.running)) ||
        activityDesc(a, b),
    );
  }
  return copy.sort((a, b) => waitingFirst(a, b) || activityDesc(a, b));
}

/** "" (a web chat) sorts after every named project rather than to the top. */
function projectKey(s: Sortable): string {
  return s.project?.trim() ? s.project.trim() : "￿";
}

/**
 * The project header to show above a row in project mode, or null when the row
 * continues the previous project. Lets a flat <ul> render grouped.
 */
export function projectHeader(
  rows: readonly Sortable[],
  i: number,
  mode: SessionSort,
): string | null {
  if (mode !== "project") return null;
  const label = rows[i].project?.trim() || "No project";
  if (i === 0) return label;
  const prev = rows[i - 1].project?.trim() || "No project";
  return prev === label ? null : label;
}
