import type { Message } from "./protocol";

/**
 * A renderable row in the chat: either a single message (user / assistant /
 * standalone tool, etc.) or a paired tool_use + tool_result.
 *
 * Pairing rule, id-first with an order-based fallback: a ``tool_use`` row is
 * paired with the FIRST subsequent ``tool_result`` whose ``content.tool_use_id``
 * matches the ``tool_use``'s ``content.id``, when both carry one. Correlation
 * ids are what make this unambiguous — with **parallel** tool calls (routine
 * for Claude) or two calls sharing a name, a flat tool_start/tool_end stream
 * has no other way to tell which result belongs to which call.
 *
 * Not every producer stamps ids yet: events already in the ledger predate
 * this field, and some runners (see `runner/canopy_runner`) don't emit it
 * until updated separately. When a ``tool_result`` carries no id at all, it
 * falls back to the FIRST still-open ``tool_use`` that *also* has no id
 * (oldest-first, FIFO) — today's pre-correlation pairing heuristic. That
 * fallback queue is tracked separately from the id map, so an id-tagged
 * stream and a legacy no-id stream can be interleaved (e.g. older turns in
 * the same session predating the id, followed by newer ones that have it)
 * without cross-pairing into each other.
 *
 * Unpaired ``tool_result`` rows (an id that matches no pending ``tool_use``,
 * or no id and nothing left in the no-id fallback queue — shouldn't happen
 * in practice, but defend) fall through as standalone messages so we never
 * silently drop content.
 */
export type ChatRow =
  | { kind: "message"; message: Message; key: string }
  | { kind: "tool_pair"; use: Message; result: Message | null; key: string };

function toolUseId(message: Message): string | null {
  const content = message.content as Record<string, unknown> | undefined;
  const id = content?.id;
  return typeof id === "string" && id !== "" ? id : null;
}

function toolResultId(message: Message): string | null {
  const content = message.content as Record<string, unknown> | undefined;
  const id = content?.tool_use_id;
  return typeof id === "string" && id !== "" ? id : null;
}

/**
 * A pending live row (`status: "pending"` from PreToolUse, no ordinal) is a
 * PLACEHOLDER. It is meant to be replaced when its result arrives — but if that
 * never lands (the turn ended, the runner stopped streaming, a hook was dropped),
 * it strands a row rendering "running…" forever, which reads as an agent stuck
 * mid-call. Observed live 2026-07-27: one orphan row per turn, and it vanished on
 * reload because the durable transcript never contained it.
 *
 * A pending row is therefore dropped once ANY later row exists — later activity
 * is proof that whatever it was waiting on is no longer in flight. It survives
 * only while it is genuinely the newest thing in the session, which is exactly
 * when "running…" is true.
 */
function dropStaleLiveRows(messages: Message[]): Message[] {
  const lastIndex = messages.length - 1;
  return messages.filter((m, i) => {
    if (m.role !== "tool_use") return true;
    const content = m.content as Record<string, unknown> | undefined;
    if (content?.status !== "pending") return true;
    const id = typeof content.id === "string" ? content.id : null;
    // Its result arrived — the pair will render normally, keep it.
    if (
      id &&
      messages.some(
        (o) =>
          o.role === "tool_result" &&
          (o.content as Record<string, unknown> | undefined)?.tool_use_id === id,
      )
    ) {
      return true;
    }
    // Still the newest row: genuinely in flight.
    return i === lastIndex;
  });
}

export function pairToolMessages(messages: Message[]): ChatRow[] {
  messages = dropStaleLiveRows(messages);
  const rows: ChatRow[] = [];
  // Map of tool_use_id → index into rows[] for that pair. Lets a tool_result
  // arriving later in the stream slot itself into the existing pair row.
  const pendingByToolId = new Map<string, number>();
  // FIFO fallback queue of row indices for tool_use rows with NO id — the
  // pre-correlation pairing heuristic, kept alive for producers/ledger rows
  // that predate tool_use_id.
  const pendingNoId: number[] = [];

  for (const m of messages) {
    if (m.role === "tool_use") {
      const id = toolUseId(m);
      const row: ChatRow = {
        kind: "tool_pair",
        use: m,
        result: null,
        key: `pair-${m.id}`,
      };
      rows.push(row);
      const idx = rows.length - 1;
      if (id !== null) {
        pendingByToolId.set(id, idx);
      } else {
        pendingNoId.push(idx);
      }
      continue;
    }
    if (m.role === "tool_result") {
      const id = toolResultId(m);
      if (id !== null) {
        const idx = pendingByToolId.get(id);
        if (idx !== undefined) {
          const row = rows[idx];
          if (row.kind === "tool_pair") {
            rows[idx] = { ...row, result: m };
            pendingByToolId.delete(id);
            continue;
          }
        }
        // An id that matches no pending id-tagged use — a genuine ghost,
        // never absorbed by the no-id fallback queue (that queue is for
        // results that carry no id of their own, not for stray ids).
        rows.push({ kind: "message", message: m, key: `msg-${m.id}` });
        continue;
      }
      // No id on the result at all — fall back to the oldest still-open
      // no-id tool_use (order-based pairing, same as before correlation
      // ids existed).
      let paired = false;
      while (pendingNoId.length > 0) {
        const idx = pendingNoId.shift() as number;
        const row = rows[idx];
        if (row.kind === "tool_pair" && row.result === null) {
          rows[idx] = { ...row, result: m };
          paired = true;
          break;
        }
      }
      if (!paired) {
        // Nothing left in the fallback queue to pair with — standalone so
        // content isn't silently dropped.
        rows.push({ kind: "message", message: m, key: `msg-${m.id}` });
      }
      continue;
    }
    rows.push({ kind: "message", message: m, key: `msg-${m.id}` });
  }
  return rows;
}

export interface ToolCallStatus {
  kind: "success" | "error" | "pending";
  label: string;
}

/** Derive a status badge for a paired tool row. */
export function deriveToolStatus(
  use: Message,
  result: Message | null,
): ToolCallStatus {
  if (result === null) {
    return { kind: "pending", label: "running…" };
  }
  if (result.status === "error" || use.status === "error") {
    return { kind: "error", label: "error" };
  }
  // Some MCP servers signal failure in the result content rather than
  // setting an HTTP-style status — sniff for the common "Error" / "error"
  // prefixes so the badge reflects reality without server changes.
  const head = (result.plaintext || "").trim().slice(0, 80).toLowerCase();
  if (head.startsWith("error") || head.startsWith("traceback")) {
    return { kind: "error", label: "error" };
  }
  return { kind: "success", label: "ok" };
}

/** One-line summary for the collapsed tool-call header. */
export function toolPreview(use: Message, result: Message | null): string {
  const name = String(
    (use.content as { name?: unknown } | undefined)?.name ?? "tool",
  );
  // Bash → command text; everything else → first 80 chars of result body.
  const input = (use.content as { input?: Record<string, unknown> } | undefined)
    ?.input;
  if (name === "Bash" && input && typeof input.command === "string") {
    return input.command.trim().split("\n")[0]?.slice(0, 100) ?? "";
  }
  if (name === "Write" && input && typeof input.file_path === "string") {
    return input.file_path;
  }
  if (name === "Read" && input && typeof input.file_path === "string") {
    return input.file_path;
  }
  if (name === "Edit" && input && typeof input.file_path === "string") {
    return input.file_path;
  }
  if (name === "TodoWrite") {
    const todos = (input as { todos?: unknown[] } | undefined)?.todos;
    if (Array.isArray(todos)) {
      return `${todos.length} todo${todos.length === 1 ? "" : "s"}`;
    }
  }
  // Skill / Agent dispatches — show what's being dispatched.
  if (name === "Skill" && input && typeof input.skill === "string") {
    return input.skill;
  }
  if (name === "Agent") {
    const desc = (input as { description?: string } | undefined)?.description;
    if (typeof desc === "string" && desc) return desc;
    const sub = (input as { subagent_type?: string } | undefined)
      ?.subagent_type;
    if (typeof sub === "string" && sub) return sub;
  }
  // MCP tools: the post-`__` segment is the most informative part.
  if (name.startsWith("mcp__")) {
    const parts = name.split("__");
    const tail = parts[parts.length - 1] ?? name;
    return tail;
  }
  // Default: a peek at the result body so the user can scan the call
  // outcome without expanding every row.
  return (result?.plaintext || "").split("\n")[0]?.slice(0, 100) ?? "";
}

/** Short display label for the tool name in the collapsed header. */
export function toolDisplayName(use: Message): string {
  const name = String(
    (use.content as { name?: unknown } | undefined)?.name ?? "tool",
  );
  if (name.startsWith("mcp__")) {
    // mcp__plugin_ace_ace-gdrive__drive_create_file → "drive_create_file"
    const parts = name.split("__");
    return parts[parts.length - 1] ?? name;
  }
  return name;
}
