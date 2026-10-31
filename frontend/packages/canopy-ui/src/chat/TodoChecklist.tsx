import { Check, ChevronRight, Loader2, X } from "lucide-react";

import type { Message } from "./protocol";
import { deriveToolStatus } from "./pairToolMessages";
import { todosOf, type Todo } from "./turnWork";

const GLYPH: Record<Todo["status"], { glyph: string; label: string; className: string }> = {
  completed: { glyph: "✓", label: "done", className: "text-success" },
  in_progress: { glyph: "◐", label: "in progress", className: "text-primary" },
  pending: { glyph: "○", label: "to do", className: "text-muted-foreground" },
};

export function todoProgress(todos: Todo[]): string {
  const done = todos.filter((t) => t.status === "completed").length;
  return `${done}/${todos.length} done`;
}

/**
 * An agent's to-do list as a checklist: ✓ done, ◐ in progress (in its
 * "doing" wording, `activeForm`), ○ still to do.
 *
 * `pinned` sticks it to the top of the transcript while its turn's work
 * scrolls beneath it — the turn's plan stays in view as the turn works through
 * it. `topOffset` clears a sticky toolbar above it.
 */
export function TodoChecklist({
  todos,
  pinned = false,
  topOffset = 0,
  bare = false,
}: {
  todos: Todo[];
  pinned?: boolean;
  topOffset?: number;
  /** No frame of its own — inside a row that already draws one. */
  bare?: boolean;
}) {
  const frame = bare
    ? ""
    : "rounded border border-border bg-card px-2.5 py-1.5 shadow-sm";
  return (
    <div
      data-testid="todo-checklist"
      className={`${frame} text-xs ${pinned ? "sticky z-[5]" : ""}`}
      style={pinned ? { top: topOffset } : undefined}
    >
      <div className="mb-1 flex items-center gap-2 text-[11px] font-medium text-muted-foreground">
        <span>To-dos</span>
        <span className="ml-auto tabular-nums">{todoProgress(todos)}</span>
      </div>
      <ul className={`space-y-0.5 ${pinned ? "max-h-40 overflow-y-auto" : ""}`}>
        {todos.map((todo, i) => {
          const g = GLYPH[todo.status];
          const text =
            todo.status === "in_progress" && todo.activeForm ? todo.activeForm : todo.content;
          return (
            <li key={i} data-status={todo.status} className="flex items-start gap-1.5 leading-snug">
              <span aria-label={g.label} className={`w-3 shrink-0 text-center ${g.className}`}>
                {g.glyph}
              </span>
              <span
                className={
                  todo.status === "completed"
                    ? "text-muted-foreground line-through"
                    : todo.status === "in_progress"
                      ? "font-medium text-foreground"
                      : "text-foreground-secondary"
                }
              >
                {text}
              </span>
            </li>
          );
        })}
      </ul>
    </div>
  );
}

/**
 * A `TodoWrite` call's row: the same one-line header as every tool row, and
 * opened, the list it set — not the JSON it was set with.
 */
export function TodoWriteRow({
  use,
  result,
  forceOpen,
}: {
  use: Message;
  result: Message | null;
  forceOpen?: boolean;
}) {
  const todos = todosOf(use) ?? [];
  const status = deriveToolStatus(use, result);
  const StatusIcon = status.kind === "success" ? Check : status.kind === "error" ? X : Loader2;
  const iconColor =
    status.kind === "success"
      ? "text-success"
      : status.kind === "error"
        ? "text-destructive"
        : "text-muted-foreground animate-spin";
  return (
    <details
      data-turn-index={use.turn_index}
      data-testid="todowrite-row"
      data-tool-status={status.kind}
      open={forceOpen}
      className="group/todo my-1 rounded border border-border bg-muted/40 text-sm"
    >
      <summary className="flex cursor-pointer items-center gap-2 px-2 py-1.5 text-muted-foreground hover:bg-muted/60 select-none [&::-webkit-details-marker]:hidden">
        <ChevronRight className="h-3 w-3 shrink-0 transition-transform group-open/todo:rotate-90" />
        <StatusIcon className={`h-3.5 w-3.5 shrink-0 ${iconColor}`} />
        <span className="font-mono text-xs font-medium text-foreground">TodoWrite</span>
        <span className="truncate font-mono text-xs text-muted-foreground">
          · {todoProgress(todos)}
        </span>
      </summary>
      <div className="border-t border-border/60 p-2">
        <TodoChecklist todos={todos} bare />
        {status.kind === "error" && result && (
          <pre className="mt-2 overflow-x-auto whitespace-pre-wrap break-all rounded bg-background p-2 text-xs text-destructive">
            {result.plaintext}
          </pre>
        )}
      </div>
    </details>
  );
}
