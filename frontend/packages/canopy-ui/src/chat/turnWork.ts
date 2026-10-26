import type { Message } from "./protocol";
import type { ChatRow } from "./pairToolMessages";
import { mcpServerLabel, toolDisplayName } from "./pairToolMessages";
import { isActionableRow } from "./groupToolRuns";

/**
 * What an agent's turn did, read off its rows — so a long turn can be followed
 * the way Claude Code's own window lets you follow one: a line saying what it
 * is doing now, its to-do list, and once it is done, one line for all the work
 * with the reply in full beneath it.
 *
 * Pure functions over `ChatRow`s, no React. Nothing here decides whether a turn
 * is RUNNING — that is the server's `turn_status` (see turnStatus.ts); the
 * caller passes it in.
 */

// ---------------------------------------------------------------------------
// Turns
// ---------------------------------------------------------------------------

/** One person's message and everything that followed it, up to the next one.
 *  `user` is null for rows before the first message (a seed, a system row). */
export interface TurnSegment {
  key: string;
  user: ChatRow | null;
  body: ChatRow[];
}

function toolName(use: Message): string {
  const name = (use.content as { name?: unknown } | undefined)?.name;
  return typeof name === "string" ? name : "";
}

function toolInput(use: Message): Record<string, unknown> {
  const input = (use.content as { input?: unknown } | undefined)?.input;
  return input && typeof input === "object" ? (input as Record<string, unknown>) : {};
}

/** A line a person sent, which starts a turn. A compaction summary is a `user`
 *  row too, but Claude Code wrote it — it is part of the work, not a new ask. */
function isPersonLine(row: ChatRow): boolean {
  if (row.kind !== "message" || row.message.role !== "user") return false;
  // A cancelled send never started anything: it sits in whatever turn it was
  // sent into, marked, rather than splitting that turn's work around itself.
  if (row.message.content?.cancelled === true) return false;
  const record = row.message.content?.record as Record<string, unknown> | undefined;
  return !record?.compact_summary;
}

export function segmentTurns(rows: ChatRow[]): TurnSegment[] {
  const out: TurnSegment[] = [];
  let current: TurnSegment | null = null;
  for (const row of rows) {
    if (isPersonLine(row)) {
      current = { key: `turn-${row.key}`, user: row, body: [] };
      out.push(current);
      continue;
    }
    if (!current) {
      current = { key: `turn-pre-${row.key}`, user: null, body: [] };
      out.push(current);
    }
    current.body.push(row);
  }
  return out;
}

/** True when a row is still on its way: a call with no result, or a reply
 *  still streaming in. */
export function rowIsInFlight(row: ChatRow): boolean {
  if (row.kind === "tool_pair") return row.result === null;
  return row.message.status === "streaming" || row.message.status === "pending";
}

// ---------------------------------------------------------------------------
// Folding a finished turn
// ---------------------------------------------------------------------------

/** A row the person may have to act on, or that records them acting: never
 *  hidden behind a fold. An MCP Apps View or a secret request (the rule
 *  groupToolRuns already keeps), and a question the agent put to them. */
export function mustStayVisible(row: ChatRow): boolean {
  if (isActionableRow(row)) return true;
  if (row.kind === "message") {
    return Boolean(row.message.app || row.message.secret_request || row.message.content?.cancelled === true);
  }
  return toolName(row.use) === "AskUserQuestion";
}

/** The agent's reply text — not a tool row, and not Claude Code's own error
 *  notice (which is not the agent answering). */
function isReplyText(row: ChatRow): boolean {
  if (row.kind !== "message") return false;
  const m = row.message;
  return m.role === "assistant" && m.plaintext.trim() !== "" && !m.content?.api_error;
}

export interface FileChange {
  path: string;
  added: number;
  removed: number;
}

export interface TurnSummary {
  /** From the person's message to the last thing the turn produced; null when
   *  the rows carry no usable times. */
  durationMs: number | null;
  toolCalls: number;
  errors: number;
  files: FileChange[];
}

export interface TurnFold {
  /** The work behind the fold line: the turn's tool calls. */
  folded: ChatRow[];
  /** Everything else before the reply, in order: the agent's own words along
   *  the way, and calls the person acts on. Never folded. */
  kept: ChatRow[];
  /** The agent's final text, shown in full below the fold. */
  reply: ChatRow;
  /** Anything after that final text (rare: a call made after it). */
  after: ChatRow[];
  summary: TurnSummary;
}

/**
 * How to fold a FINISHED turn, or null to show it as it is.
 *
 * `live` is the caller's answer to "is this turn still running" — from the
 * server's turn status. A live turn is never folded: you are watching it.
 * Neither is a turn with no reply text (there is nothing to show beneath the
 * fold) or with no tool calls in front of its reply.
 *
 * Only TOOL CALLS fold. What the agent said along the way stays where it can be
 * read: it was on screen while the turn ran, and folding it at the end pulled
 * text out from under someone in the middle of reading it.
 */
export function planFold(segment: TurnSegment, { live }: { live: boolean }): TurnFold | null {
  if (live || !segment.user) return null;
  const body = segment.body;
  if (body.some(rowIsInFlight)) return null;
  let replyAt = -1;
  for (let i = body.length - 1; i >= 0; i--) {
    if (isReplyText(body[i])) {
      replyAt = i;
      break;
    }
  }
  if (replyAt < 0) return null;
  const before = body.slice(0, replyAt);
  const folds = (r: ChatRow) => r.kind === "tool_pair" && !mustStayVisible(r);
  const folded = before.filter(folds);
  if (folded.length === 0) return null;
  return {
    folded,
    kept: before.filter((r) => !folds(r)),
    reply: body[replyAt],
    after: body.slice(replyAt + 1),
    summary: summariseTurn(segment),
  };
}

function rowFailed(row: ChatRow): boolean {
  return (
    row.kind === "tool_pair" &&
    (row.result?.status === "error" ||
      (row.result?.content as { is_error?: unknown } | undefined)?.is_error === true)
  );
}

function rowMessages(row: ChatRow): Message[] {
  if (row.kind === "message") return [row.message];
  return row.result ? [row.use, row.result] : [row.use];
}

function timeOf(iso: string | null | undefined): number {
  if (!iso) return NaN;
  return Date.parse(iso);
}

export function summariseTurn(segment: TurnSegment): TurnSummary {
  const start = segment.user?.kind === "message" ? timeOf(segment.user.message.created_at) : NaN;
  let end = NaN;
  for (const row of segment.body) {
    for (const m of rowMessages(row)) {
      const t = timeOf(m.completed_at ?? m.created_at);
      if (!Number.isNaN(t) && (Number.isNaN(end) || t > end)) end = t;
    }
  }
  const durationMs = !Number.isNaN(start) && !Number.isNaN(end) && end >= start ? end - start : null;
  const tools = segment.body.filter((r) => r.kind === "tool_pair");
  return {
    durationMs,
    toolCalls: tools.length,
    errors: tools.filter(rowFailed).length,
    files: filesChanged(segment.body),
  };
}

/** "Worked for 4m 12s · 23 tool calls · 3 files changed". */
export function foldLine(summary: TurnSummary): string {
  const parts: string[] = [];
  parts.push(summary.durationMs != null ? `Worked for ${formatElapsed(summary.durationMs)}` : "Worked");
  if (summary.toolCalls > 0) {
    parts.push(`${summary.toolCalls} tool call${summary.toolCalls === 1 ? "" : "s"}`);
  }
  if (summary.files.length > 0) {
    parts.push(`${summary.files.length} file${summary.files.length === 1 ? "" : "s"} changed`);
  }
  return parts.join(" · ");
}

/** "12s" / "4m 12s" / "1h 5m" — a running clock, not a timestamp. */
export function formatElapsed(ms: number): string {
  const total = Math.max(0, Math.floor(ms / 1000));
  if (total < 60) return `${total}s`;
  const mins = Math.floor(total / 60);
  if (mins < 60) return `${mins}m ${total % 60}s`;
  return `${Math.floor(mins / 60)}h ${mins % 60}m`;
}

// ---------------------------------------------------------------------------
// Files changed
// ---------------------------------------------------------------------------

/** Lines in a block of text, a trailing newline not counting as one more. */
export function countLines(text: unknown): number {
  if (typeof text !== "string" || text === "") return 0;
  const body = text.endsWith("\n") ? text.slice(0, -1) : text;
  return body.split("\n").length;
}

/**
 * Every file the turn's edit calls touched, in first-touched order, with the
 * lines they added and removed — counted from the call's own input
 * (`old_string`/`new_string`/`content`), so a Write counts its whole file as
 * added: the input does not say what was there before. A call that failed
 * changed nothing and is left out.
 */
export function filesChanged(rows: ChatRow[]): FileChange[] {
  const byPath = new Map<string, FileChange>();
  const touch = (path: unknown, added: number, removed: number) => {
    if (typeof path !== "string" || !path) return;
    const prior = byPath.get(path);
    if (prior) {
      prior.added += added;
      prior.removed += removed;
    } else {
      byPath.set(path, { path, added, removed });
    }
  };
  for (const row of rows) {
    if (row.kind !== "tool_pair" || rowFailed(row)) continue;
    const name = toolName(row.use);
    const input = toolInput(row.use);
    switch (name) {
      case "Edit":
        touch(input.file_path, countLines(input.new_string), countLines(input.old_string));
        break;
      case "MultiEdit": {
        let added = 0;
        let removed = 0;
        for (const e of Array.isArray(input.edits) ? input.edits : []) {
          const edit = (e ?? {}) as Record<string, unknown>;
          added += countLines(edit.new_string);
          removed += countLines(edit.old_string);
        }
        touch(input.file_path, added, removed);
        break;
      }
      case "Write":
        touch(input.file_path, countLines(input.content), 0);
        break;
      case "NotebookEdit":
        touch(
          input.notebook_path,
          input.edit_mode === "delete" ? 0 : countLines(input.new_source),
          0,
        );
        break;
      default:
        break;
    }
  }
  return [...byPath.values()];
}

// ---------------------------------------------------------------------------
// To-dos
// ---------------------------------------------------------------------------

export type TodoStatus = "pending" | "in_progress" | "completed";

export interface Todo {
  content: string;
  status: TodoStatus;
  activeForm?: string;
}

/** The list a `TodoWrite` call set, or null when the row is not one. */
export function todosOf(use: Message): Todo[] | null {
  if (toolName(use) !== "TodoWrite") return null;
  const todos = toolInput(use).todos;
  if (!Array.isArray(todos)) return null;
  return todos
    .filter((t): t is Record<string, unknown> => Boolean(t) && typeof t === "object")
    .map((t) => ({
      content: typeof t.content === "string" ? t.content : "",
      status:
        t.status === "completed" || t.status === "in_progress" ? t.status : ("pending" as const),
      activeForm: typeof t.activeForm === "string" ? t.activeForm : undefined,
    }));
}

/** The turn's to-do list as it stands: its LAST successful `TodoWrite`. Each
 *  call replaces the whole list, so the last one is the list. */
export function latestTodos(rows: ChatRow[]): Todo[] | null {
  for (let i = rows.length - 1; i >= 0; i--) {
    const row = rows[i];
    if (row.kind !== "tool_pair" || rowFailed(row)) continue;
    const todos = todosOf(row.use);
    if (todos) return todos;
  }
  return null;
}

// ---------------------------------------------------------------------------
// What it is doing now
// ---------------------------------------------------------------------------

export interface ActivityText {
  verb: string;
  /** The thing it is acting on — a command, a path — shown in monospace. */
  target?: string;
}

function firstLine(text: unknown, max = 80): string {
  if (typeof text !== "string") return "";
  const line = text.trim().split("\n")[0] ?? "";
  return line.length > max ? `${line.slice(0, max - 1)}…` : line;
}

/** In words, what one call is doing: "Running `npm test`", "Editing apps/x.py". */
export function describeCall(use: Message): ActivityText {
  const name = toolName(use);
  const input = toolInput(use);
  const target = (v: unknown) => firstLine(v) || undefined;
  switch (name) {
    case "Bash":
      return { verb: "Running", target: target(input.command) };
    case "Edit":
    case "MultiEdit":
      return { verb: "Editing", target: target(input.file_path) };
    case "Write":
      return { verb: "Writing", target: target(input.file_path) };
    case "NotebookEdit":
      return { verb: "Editing", target: target(input.notebook_path) };
    case "Read":
      return { verb: "Reading", target: target(input.file_path) };
    case "Grep":
      return { verb: "Searching for", target: target(input.pattern) };
    case "Glob":
      return { verb: "Finding files", target: target(input.pattern) };
    case "WebFetch":
      return { verb: "Fetching", target: target(input.url) };
    case "WebSearch":
      return { verb: "Searching the web for", target: target(input.query) };
    case "TodoWrite":
      return { verb: "Updating the to-do list" };
    case "Agent":
    case "Task":
      return { verb: "Running an agent:", target: target(input.description ?? input.subagent_type) };
    case "Skill":
      return { verb: "Using skill", target: target(input.skill) };
    default:
      break;
  }
  if (name.startsWith("mcp__")) {
    const server = mcpServerLabel(name);
    return { verb: server ? `Calling ${server}:` : "Calling", target: toolDisplayName(use) };
  }
  return { verb: "Running", target: name || "a tool" };
}

/** What the turn is doing right now: its newest call still running, or null
 *  when nothing is (it is thinking, or writing its reply). */
export function currentActivity(rows: ChatRow[]): ActivityText | null {
  for (let i = rows.length - 1; i >= 0; i--) {
    const row = rows[i];
    if (row.kind === "tool_pair" && row.result === null) return describeCall(row.use);
  }
  return null;
}
