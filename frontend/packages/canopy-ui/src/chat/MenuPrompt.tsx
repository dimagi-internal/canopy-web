import type { SessionMenu } from "./protocol";

export interface MenuPromptProps {
  menu: SessionMenu;
  busy?: boolean;
  error?: string;
  onAnswer: (option: number | null) => void;
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
 * The options render in one of two shapes, chosen by whether the dialog carries
 * descriptions. A permission prompt's options ("Yes" / "No") are a row of chips.
 * An AskUserQuestion's are not: its descriptions are the decision itself — on
 * the run that motivated this, "Proceed to Phase 4" and "Stop the run here"
 * were separated entirely by prose the labels did not contain (that Phase 4 is
 * test-gated and reaches no real payment). Rendering those as bare chips asks
 * somebody to choose blind, which is the same failure as showing no menu, only
 * quieter.
 */
export function MenuPrompt({ menu, busy = false, error, onAnswer }: MenuPromptProps) {
  const described = menu.options.some((o) => o.description);
  return (
    <div className="rounded-md border border-warning/30 bg-warning/10 px-3 py-2.5 text-sm">
      <div className="flex items-center gap-1.5 font-medium text-warning">
        <span className="inline-block h-1.5 w-1.5 animate-pulse rounded-full bg-warning" />
        {menu.title || "Waiting on you"}
      </div>
      {menu.body ? (
        <pre className="mt-1.5 max-h-32 overflow-auto whitespace-pre-wrap break-all rounded bg-muted px-2 py-1.5 font-mono text-[12px] text-foreground-secondary">
          {menu.body}
        </pre>
      ) : null}
      <div className="mt-2 text-foreground">{menu.question}</div>
      {described ? (
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
      {error ? <div className="mt-1.5 text-[12px] text-destructive">{error}</div> : null}
    </div>
  );
}
