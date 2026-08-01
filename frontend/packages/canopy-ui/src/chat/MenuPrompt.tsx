import type { SessionMenu } from "./protocol";

export interface MenuPromptProps {
  menu: SessionMenu;
  busy?: boolean;
  error?: string;
  onAnswer: (option: number | null) => void;
  /** Injectable for tests; defaults to the wall clock. */
  now?: number;
}

/** How old a dialog may be before we say so. The runner re-reports every ~10s,
 *  so anything past a couple of minutes is not merely lagging — it is a dialog
 *  nobody has confirmed lately, and the honest thing is to show its age rather
 *  than let a confident-looking button be the way you discover it. */
const STALE_AFTER_MS = 120_000;

export function menuAge(menu: SessionMenu, now: number): string {
  if (!menu.observed_at) return "";
  const ms = now - menu.observed_at * 1000;
  if (ms < STALE_AFTER_MS) return "";
  const mins = Math.round(ms / 60_000);
  return mins < 60 ? `last seen ${mins}m ago` : `last seen ${Math.round(mins / 60)}h ago`;
}

/**
 * The dialog a blocked agent is waiting on, answerable from here.
 *
 * Shows the SUBJECT, not just the question: "Do you want to proceed?" tells you
 * nothing away from the keyboard — the command is the whole decision, so it is
 * rendered verbatim and monospaced rather than summarised.
 *
 * Refusing is always offered separately from the numbered options. Every dialog
 * accepts Escape, and it is the only answer that stays correct if the dialog on
 * screen is not the one rendered here.
 *
 * A tap that was relayed and then refused BY THE RUNNER comes back as
 * `menu.answer_note`, and it wins over the local `error` prop because it is the
 * later, more specific half of the same story: `error` means the server would
 * not relay the tap, `answer_note` means it did and the keystroke still did not
 * land. Showing neither is what made this button look dead for 45 minutes — the
 * API answers `ok:true` the instant it relays, so silence read as success.
 *
 * The options render in one of two shapes, chosen by whether the dialog carries
 * descriptions. A permission prompt's options ("Yes" / "No") are a row of chips.
 * An AskUserQuestion's are not: its descriptions are the decision itself — on
 * the run that motivated this, "Proceed to Phase 4" and "Stop the run here"
 * were separated entirely by prose the labels did not contain (that Phase 4 is
 * test-gated and reaches no real payment). Rendering those as bare chips asks
 * somebody to choose blind, which is the same failure as showing no menu, only
 * quieter.
 */
export function MenuPrompt({ menu, busy = false, error, onAnswer, now = Date.now() }: MenuPromptProps) {
  const described = menu.options.some((o) => o.description);
  // A dialog Claude Code drew with no tool call behind it — a permission prompt,
  // a trust gate. The `Notification` hook says a human is wanted and carries a
  // message, but no options: reading those means driving CDP, which steals
  // focus, so they are genuinely not available here. Saying so beats an empty
  // box, and beats the previous behaviour of showing nothing at all.
  //
  // Escape is still REAL on one of these: the runner re-reads the actual screen
  // before pressing anything, and a permission prompt parses there — so the
  // refuse button below is an action, not a placeholder.
  const optionless = menu.options.length === 0;
  const age = menuAge(menu, now);
  return (
    <div className="rounded-md border border-warning/30 bg-warning/10 px-3 py-2.5 text-sm">
      <div className="flex items-center gap-1.5 font-medium text-warning">
        <span className="inline-block h-1.5 w-1.5 animate-pulse rounded-full bg-warning" />
        {menu.title || "Waiting on you"}
        {age ? (
          <span className="ml-auto text-[11px] font-normal text-muted-foreground">{age}</span>
        ) : null}
      </div>
      {menu.body ? (
        <pre className="mt-1.5 max-h-32 overflow-auto whitespace-pre-wrap break-all rounded bg-muted px-2 py-1.5 font-mono text-[12px] text-foreground-secondary">
          {menu.body}
        </pre>
      ) : null}
      <div className="mt-2 text-foreground">{menu.question}</div>
      {optionless ? (
        <div className="mt-1.5 text-[12px] leading-snug text-muted-foreground">
          The options can only be read at the keyboard — open this session in emdash to
          pick one. Cancelling from here still works.
        </div>
      ) : described ? (
        <div className="mt-2 flex flex-col gap-1.5">
          {menu.options.map((option) => (
            <button
              key={option.number}
              type="button"
              disabled={busy}
              onClick={() => onAnswer(option.number)}
              className="rounded border border-input bg-card px-2.5 py-2 text-left hover:bg-muted disabled:opacity-50"
            >
              <div className="text-[13px] font-medium text-foreground">{option.label}</div>
              {option.description ? (
                <div className="mt-0.5 text-[12px] leading-snug text-muted-foreground">
                  {option.description}
                </div>
              ) : null}
            </button>
          ))}
        </div>
      ) : (
        <div className="mt-2 flex flex-wrap gap-1.5">
          {menu.options.map((option) => (
            <button
              key={option.number}
              type="button"
              disabled={busy}
              onClick={() => onAnswer(option.number)}
              className="rounded border border-input bg-card px-2 py-1 text-[13px] text-foreground hover:bg-muted disabled:opacity-50"
            >
              {option.label}
            </button>
          ))}
        </div>
      )}
      <div className="mt-2">
        <button
          type="button"
          disabled={busy}
          onClick={() => onAnswer(null)}
          className="rounded px-2 py-1 text-[13px] text-muted-foreground hover:text-foreground disabled:opacity-50"
        >
          Cancel (Esc)
        </button>
      </div>
      {menu.answer_note || error ? (
        <div className="mt-1.5 text-[12px] text-destructive">{menu.answer_note || error}</div>
      ) : null}
    </div>
  );
}
