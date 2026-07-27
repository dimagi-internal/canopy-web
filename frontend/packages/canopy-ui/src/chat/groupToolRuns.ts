import type { ChatRow } from "./pairToolMessages";

/**
 * Collapse a run of consecutive tool calls into ONE row.
 *
 * An agent working on something emits long stretches of back-to-back tool
 * calls. Rendered one per row they push everything you actually read — the
 * agent's prose, your own messages — off the screen, and a session you glance
 * at becomes a wall of `Bash` you have to scroll past. Claude Code's own answer
 * is a single "Running 5 shell commands…" line you can open if you care, and
 * this is that.
 *
 * A run is broken by any non-tool row, so prose always separates groups and the
 * conversation stays legible. Runs of one are left alone: wrapping a single
 * call in a group adds a click without hiding anything.
 */
export type GroupedRow =
  | ChatRow
  | { kind: "tool_run"; rows: ChatRow[]; key: string };

/** Below this, a run renders as individual rows — grouping one or two calls
 *  costs a click and saves no space. */
export const MIN_RUN_TO_GROUP = 3;

export function groupToolRuns(
  rows: ChatRow[],
  minRun: number = MIN_RUN_TO_GROUP,
): GroupedRow[] {
  const out: GroupedRow[] = [];
  let run: ChatRow[] = [];

  const flush = () => {
    if (run.length === 0) return;
    if (run.length >= minRun) {
      out.push({ kind: "tool_run", rows: run, key: `run-${run[0].key}` });
    } else {
      out.push(...run);
    }
    run = [];
  };

  for (const row of rows) {
    if (row.kind === "tool_pair") {
      run.push(row);
      continue;
    }
    flush();
    out.push(row);
  }
  flush();
  return out;
}

/** True when any call in the run is still running — a collapsed group should say
 *  so, or an agent mid-task looks idle. */
export function runIsActive(rows: ChatRow[]): boolean {
  return rows.some((row) => row.kind === "tool_pair" && row.result === null);
}

/** A short label for a collapsed run: "5 tool calls · Bash, Read". */
export function summariseRun(rows: ChatRow[]): string {
  const names = new Set<string>();
  for (const row of rows) {
    if (row.kind !== "tool_pair") continue;
    const name = (row.use.content as { name?: unknown } | undefined)?.name;
    if (typeof name === "string" && name) names.add(name);
  }
  const kinds = [...names].slice(0, 3).join(", ");
  const plural = rows.length === 1 ? "call" : "calls";
  return kinds ? `${rows.length} tool ${plural} · ${kinds}` : `${rows.length} tool ${plural}`;
}

/** True when any call in the run failed — a collapsed run must not hide an
 *  error, or you'd scroll past the one thing worth stopping for. */
export function runHasError(rows: ChatRow[]): boolean {
  return rows.some(
    (row) =>
      row.kind === "tool_pair" &&
      (row.result?.status === "error" ||
        (row.result?.content as { is_error?: unknown } | undefined)?.is_error === true),
  );
}
