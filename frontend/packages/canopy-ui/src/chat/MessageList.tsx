import { useContext, useMemo, type ReactNode } from "react";
import { ChevronRight, Loader2 } from "lucide-react";

import type { Message } from "./protocol";
import type { RenderMarkdown } from "./MessageItem";
import { MessageItem } from "./MessageItem";
import { ToolCallPair } from "./ToolCallPair";
import { ActivityLine } from "./ActivityLine";
import { TodoChecklist, TodoWriteRow } from "./TodoChecklist";
import { TurnFold } from "./TurnFold";
import { AppView } from "./mcpApps/AppView";
import { LatestAppViewContext } from "./mcpApps/context";
import { useSecretRequestHost } from "./secretRequest";
import { SubagentsContext, startedRunningAgent } from "./subagents";
import { pairToolMessages, type ChatRow } from "./pairToolMessages";
import {
  groupToolRuns,
  lastTurnIndex,
  runHasError,
  runIsActive,
  summariseRun,
  type GroupedRow,
} from "./groupToolRuns";
import {
  currentActivity,
  latestTodos,
  planFold,
  rowIsInFlight,
  segmentTurns,
  todosOf,
} from "./turnWork";

interface Props {
  messages: Message[];
  /** Rendered when there are no messages yet (replaces ace's WelcomePanel). */
  emptyState?: ReactNode;
  renderMarkdown?: RenderMarkdown;
  /**
   * The agent has the floor but has produced no row yet — render a trailing
   * activity line at the bottom of the conversation.
   *
   * A trailing ELEMENT, not a row spliced into `messages`: the message array is
   * a projection of the durable record and must keep exactly one writer, and a
   * placeholder with a made-up id would need upsert/eviction rules against every
   * frame that can follow it. This has none — it is on screen while the flag is
   * true and gone when it isn't.
   */
  pendingReply?: boolean;
  /** Wording for a QUEUED turn's line — the caller knows whether the turn is
   *  still queued for a runner and behind what. */
  pendingLabel?: string;
  /** For the pending line: a runner has the turn (vs still queued). */
  pendingWorking?: boolean;
  /** For the pending line: the runner holding the turn. */
  pendingRunner?: string | null;
  /** For the pending line: when the turn started (ISO). Defaults to the newest
   *  person's message. */
  pendingStartedAt?: string | null;
  /** Offer Stop on the pending line. */
  onStopTurn?: () => void;
  /** A stop has been asked for and not yet landed. */
  stopping?: boolean;
  /** Whether the LAST turn is still running — from the server's turn status
   *  (ChatPanel derives it). A running turn shows its work as it happens; a
   *  finished one folds its work into one line above its reply. Omitted, a
   *  turn counts as running while any of its rows is still in flight. */
  liveTurn?: boolean;
  /** Threaded to MessageItem so a `user` row can tell "me" from "someone
   *  else" for the authored-bubble treatment. */
  currentUserId?: number | null;
  /** The viewer when it is a contact; see MessageItem. */
  currentContactId?: number | null;
  /** Your own sends still queued, by client_id → turn id: their rows get a
   *  Cancel (`onCancelQueued`). */
  cancellable?: Map<string, string>;
  onCancelQueued?: (turnId: string) => void;
  /** Every tool row open (`all`) or closed (`none`) at once; `default` (or
   *  omitted) leaves each row to its own toggle. The host offers the choice —
   *  canopy's session ⋯ menu. */
  toolRows?: ToolRowsView;
}

export type ToolRowsView = "default" | "all" | "none";

/** One tool row — a `TodoWrite` as its checklist, anything else as itself. */
function ToolRow({
  use,
  result,
  forceOpen,
}: {
  use: Message;
  result: Message | null;
  forceOpen?: boolean;
}) {
  if (todosOf(use)) return <TodoWriteRow use={use} result={result} forceOpen={forceOpen} />;
  return <ToolCallPair use={use} result={result} forceOpen={forceOpen} />;
}

export function MessageList({
  messages,
  emptyState,
  renderMarkdown,
  pendingReply = false,
  pendingLabel,
  pendingWorking = false,
  pendingRunner,
  pendingStartedAt,
  onStopTurn,
  stopping,
  liveTurn,
  currentUserId,
  currentContactId,
  cancellable,
  onCancelQueued,
  toolRows = "default",
}: Props) {
  const secretHost = useSecretRequestHost();
  const subagents = useContext(SubagentsContext);
  const paired = useMemo(() => pairToolMessages(messages), [messages]);
  // One person's message and what followed it — the unit a finished turn
  // folds by. Within a turn, back-to-back tool calls still collapse into one
  // row (groupToolRuns): an agent mid-task emits long stretches of them, and
  // one row each pushes the prose you actually read off the screen.
  const turns = useMemo(() => segmentTurns(paired), [paired]);
  // The newest View opens expanded and every earlier one collapsed.
  const latestApp = useMemo(() => {
    for (let i = messages.length - 1; i >= 0; i--) {
      const app = messages[i].role === "tool_result" ? messages[i].app : null;
      if (app) return app.tool_call_id;
    }
    return null;
  }, [messages]);

  if (messages.length === 0 && !pendingReply) {
    return <>{emptyState ?? null}</>;
  }
  const forceToolOpen =
    toolRows === "all" ? true : toolRows === "none" ? false : undefined;
  const lastTurn = turns.length > 0 ? turns[turns.length - 1] : null;
  const lastUserAt =
    lastTurn?.user?.kind === "message" ? lastTurn.user.message.created_at : null;

  const renderRow = (row: GroupedRow): ReactNode => {
    if (row.kind === "tool_run") {
      // One line for a whole run, open on demand. `open` when the bulk
      // toggle says so, and always when something in it failed — a
      // collapsed group must never hide an error.
      const failed = runHasError(row.rows);
      // A run with a call still in flight must say so: collapsed, an agent
      // mid-task would otherwise look idle.
      const active = runIsActive(row.rows);
      return (
        <details
          key={row.key}
          data-turn-index={lastTurnIndex(row.rows)}
          open={forceToolOpen ?? (failed || undefined)}
          className="group/run my-1 rounded border border-border bg-muted/40 text-sm"
        >
          <summary className="flex cursor-pointer items-center gap-2 px-2 py-1.5 text-muted-foreground hover:bg-muted/60 select-none [&::-webkit-details-marker]:hidden">
            <ChevronRight className="h-3 w-3 shrink-0 transition-transform group-open/run:rotate-90" />
            <span className="text-xs font-medium text-foreground">
              {summariseRun(row.rows)}
            </span>
            {active && (
              <span className="flex items-center gap-1 text-xs text-muted-foreground">
                <Loader2 className="h-3 w-3 animate-spin" /> running
              </span>
            )}
            {failed && (
              <span className="text-xs font-medium text-destructive">
                · contains an error
              </span>
            )}
          </summary>
          <div className="space-y-1 border-t border-border/60 p-2">
            {row.rows.map((r) =>
              r.kind === "tool_pair" ? (
                <ToolRow key={r.key} use={r.use} result={r.result} forceOpen={forceToolOpen} />
              ) : null,
            )}
          </div>
        </details>
      );
    }
    if (row.kind === "tool_pair") {
      const pair = (
        <ToolRow key={row.key} use={row.use} result={row.result} forceOpen={forceToolOpen} />
      );
      // MCP Apps: the View first, the (collapsed) tool row below it.
      if (row.result?.app) {
        return (
          <div key={row.key}>
            <AppView app={row.result.app} />
            {pair}
          </div>
        );
      }
      // The agent asked for a secret: the host's seal card, then the row.
      if (row.result?.secret_request && secretHost) {
        return (
          <div key={row.key} data-testid="secret-request-row">
            {secretHost.render(row.result.secret_request)}
            {pair}
          </div>
        );
      }
      return pair;
    }
    const item = (
      <MessageItem
        key={row.key}
        message={row.message}
        forceToolOpen={forceToolOpen}
        renderMarkdown={renderMarkdown}
        currentUserId={currentUserId}
        currentContactId={currentContactId}
      />
    );
    const clientId = row.message.role === "user" ? row.message.content?.client_id : undefined;
    const queuedTurn = typeof clientId === "string" ? cancellable?.get(clientId) : undefined;
    if (!queuedTurn || !onCancelQueued) return item;
    return (
      <div key={row.key} className="flex flex-col">
        {item}
        <div className="flex justify-end gap-2 text-[11px] text-muted-foreground">
          <span>queued</span>
          <button type="button" onClick={() => onCancelQueued(queuedTurn)}
                  data-testid="queued-cancel"
                  className="underline hover:text-foreground">
            Cancel
          </button>
        </div>
      </div>
    );
  };
  const renderRows = (list: ChatRow[]) => groupToolRuns(list).map(renderRow);

  return (
    <LatestAppViewContext.Provider value={latestApp}>
    <div className="flex flex-col">
      <div className="flex flex-col gap-2 p-4">
        {turns.map((turn, i) => {
          // Running = the caller says so (the server's turn status), or one of
          // its rows is still on its way, or an agent it started still runs. A
          // turn that hands work to background agents stops and wakes again
          // each time one reports back; folding at every stop collapsed and
          // re-expanded its work under a person reading it.
          const live =
            i === turns.length - 1 &&
            ((liveTurn ?? turn.body.some(rowIsInFlight)) ||
              startedRunningAgent(
                turn.body.flatMap((r) => (r.kind === "tool_pair" ? [r.use] : [])),
                subagents,
              ));
          const fold = planFold(turn, { live });
          const todos = latestTodos(turn.body);
          return (
            <div key={turn.key} data-testid="turn" className="flex flex-col gap-2">
              {turn.user && renderRow(turn.user)}
              {fold ? (
                <>
                  <TurnFold summary={fold.summary} forceOpen={forceToolOpen}>
                    {todos && <TodoChecklist todos={todos} />}
                    {renderRows(fold.folded)}
                  </TurnFold>
                  {renderRows(fold.kept)}
                  {renderRow(fold.reply)}
                  {renderRows(fold.after)}
                </>
              ) : (
                <>
                  {/* The turn's plan at the top of its work — pinned while it
                      runs, so the list stays in view as the work scrolls. */}
                  {todos && turn.user && (
                    <TodoChecklist todos={todos} pinned={live} />
                  )}
                  {renderRows(turn.body)}
                </>
              )}
            </div>
          );
        })}
        {pendingReply && (
          <ActivityLine
            working={pendingWorking}
            queuedLabel={pendingLabel}
            activity={lastTurn ? currentActivity(lastTurn.body) : null}
            startedAt={pendingStartedAt ?? lastUserAt}
            runner={pendingRunner}
            onStop={onStopTurn}
            stopping={stopping}
          />
        )}
      </div>
    </div>
    </LatestAppViewContext.Provider>
  );
}
