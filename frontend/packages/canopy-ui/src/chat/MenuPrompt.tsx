import { useMemo, useState } from "react";
import type { MenuQuestion, SessionMenu } from "./protocol";

export interface MenuPromptProps {
  menu: SessionMenu;
  busy?: boolean;
  error?: string;
  /** `selections` is the whole answer — one list of chosen option numbers per
   *  question. Omitted for the single-question single-select dialogs that a
   *  lone `option` has always been able to answer.
   *
   *  `texts` carries the answer that is NOT on the menu, per question. The TUI
   *  appends a "Type something" row to every question, and it is frequently
   *  where the real answer goes — the July closeout's notes were all typed, not
   *  picked. Without it a phone can only choose from what the agent guessed. */
  onAnswer: (
    option: number | null,
    selections?: number[][] | null,
    texts?: (string | null)[] | null,
  ) => void;
  /** Injectable for tests; defaults to the wall clock. */
  now?: number;
}

/** Whether this ask needs the full form rather than a row of buttons.
 *
 *  One single-select question is the shape a lone keypress answers, and it is
 *  the overwhelmingly common one — a permission prompt, a yes/no. Anything else
 *  (several questions, or one that takes several answers) cannot be completed by
 *  pressing a single button, so rendering buttons for it is a lie. */
export function needsForm(menu: SessionMenu): boolean {
  const qs = menu.questions;
  if (!qs || qs.length === 0) return false;
  return qs.length > 1 || qs.some((q) => q.multi_select);
}

function isAnswered(picks: number[] | undefined): boolean {
  return Boolean(picks && picks.length > 0);
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
/** Every question at once, answered in one go.
 *
 *  Deliberately NOT a tab strip mirroring the terminal's. The terminal shows one
 *  question at a time because it has one screen and a cursor; a phone has a
 *  scroll, and the thing that actually went wrong was somebody answering what
 *  looked like the whole ask and it turning out to be tab 2 of 3. Showing all of
 *  them, with one Send at the end, makes "have I finished?" answerable by
 *  looking — which is the only question this surface has ever got wrong.
 */
function AnswerForm({
  menu,
  questions,
  busy,
  onAnswer,
}: {
  menu: SessionMenu;
  questions: MenuQuestion[];
  busy: boolean;
  onAnswer: MenuPromptProps["onAnswer"];
}) {
  const [picks, setPicks] = useState<Record<number, number[]>>({});
  const [typed, setTyped] = useState<Record<number, string>>({});

  const toggle = (q: MenuQuestion, number: number) => {
    setPicks((prev) => {
      const current = prev[q.index] ?? [];
      if (!q.multi_select) return { ...prev, [q.index]: [number] };
      return {
        ...prev,
        [q.index]: current.includes(number)
          ? current.filter((n) => n !== number)
          : [...current, number].sort((a, b) => a - b),
      };
    });
  };

  // Typing your own answer to a SINGLE-select question replaces the pick,
  // because that is what the TUI does — selecting "Type something" moves the
  // selection onto the text row. On a multi-select the text is an extra
  // checkbox, so both can stand.
  const write = (q: MenuQuestion, value: string) => {
    setTyped((prev) => ({ ...prev, [q.index]: value }));
    if (!q.multi_select && value.trim()) {
      setPicks((prev) => ({ ...prev, [q.index]: [] }));
    }
  };

  const answered = (q: MenuQuestion) =>
    isAnswered(picks[q.index]) || Boolean((typed[q.index] ?? "").trim());

  const remaining = useMemo(
    () => questions.filter((q) => !answered(q)).length,
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [questions, picks, typed],
  );

  const send = () => {
    // Positional, and every question gets an entry even when unanswered: the
    // runner walks the tabs in this order, so a missing entry would silently
    // shift every later answer onto the wrong question.
    const selections = questions.map((q) => picks[q.index] ?? []);
    const texts = questions.map((q) => (typed[q.index] ?? "").trim() || null);
    // `option` is the first pick, for a runner too old to read `selections`.
    // Sending null there would read as "refuse" and cancel the dialog.
    onAnswer(selections[0]?.[0] ?? null, selections, texts);
  };

  return (
    <div className="mt-2 flex flex-col gap-3">
      {questions.map((q) => (
        <div key={q.index}>
          <div className="text-[12px] font-medium uppercase tracking-wide text-muted-foreground">
            {q.header || `Question ${q.index + 1}`}
            {q.multi_select ? " · pick any" : ""}
          </div>
          <div className="mt-0.5 text-[13px] text-foreground">{q.question}</div>
          <div className="mt-1.5 flex flex-col gap-1">
            {q.options.map((option) => {
              const on = (picks[q.index] ?? []).includes(option.number);
              return (
                <button
                  key={option.number}
                  type="button"
                  disabled={busy}
                  aria-pressed={on}
                  onClick={() => toggle(q, option.number)}
                  className={`flex items-start gap-2 rounded border px-2.5 py-2 text-left disabled:opacity-50 ${
                    on ? "border-warning bg-warning/10" : "border-input bg-card hover:bg-muted"
                  }`}
                >
                  <span aria-hidden className="mt-[1px] shrink-0 text-[13px]">
                    {on ? (q.multi_select ? "☑" : "◉") : q.multi_select ? "☐" : "○"}
                  </span>
                  <span>
                    <span className="block text-[13px] font-medium text-foreground">
                      {option.label}
                    </span>
                    {option.description ? (
                      <span className="mt-0.5 block text-[12px] leading-snug text-muted-foreground">
                        {option.description}
                      </span>
                    ) : null}
                  </span>
                </button>
              );
            })}
            {/* The answer that is not on the menu. The terminal offers this on
                every question ("Type something"), and it is where the real
                answer often goes, so a phone without it can only pick from what
                the agent happened to guess.
 */}
            <input
              type="text"
              value={typed[q.index] ?? ""}
              disabled={busy}
              onChange={(e) => write(q, e.target.value)}
              placeholder="…or type your own answer"
              className="mt-0.5 rounded border border-input bg-card px-2.5 py-2 text-[13px] text-foreground placeholder:text-muted-foreground disabled:opacity-50"
            />
          </div>
        </div>
      ))}
      <div className="flex flex-wrap items-center gap-2">
        <button
          type="button"
          // Sendable as soon as ANYTHING is answered, not only when everything
          // is. The terminal submits a partly-filled ask and merely warns, and
          // refusing here made a question you meant to skip unskippable from a
          // phone. Still not sendable when NOTHING is answered — that is what
          // Cancel says, and says better.
          disabled={busy || remaining === questions.length}
          onClick={send}
          className="rounded bg-warning px-3 py-1.5 text-[13px] font-medium text-warning-foreground disabled:opacity-50"
        >
          {busy ? "Sending…" : "Send answers"}
        </button>
        {remaining > 0 ? (
          <span className="text-[12px] text-muted-foreground">
            {remaining === questions.length
              ? "answer at least one to send"
              : `${remaining} unanswered — will send anyway`}
          </span>
        ) : null}
      </div>
      {menu.body ? (
        <div className="text-[12px] text-muted-foreground">{menu.body}</div>
      ) : null}
    </div>
  );
}

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
  // refuse button below is an action, not a placeholder. When the screen turns
  // out to be an ordinary prompt, that same re-read answers NO_DIALOG and the
  // marker is dropped, which is the way out of a marker that was never a dialog.
  //
  // It may also not be a dialog at all — nothing was parsed to produce this, so
  // the copy no longer promises one is there. The composer stays live behind it
  // for the same reason (`menuBlocksComposer`).
  const optionless = menu.options.length === 0;
  const age = menuAge(menu, now);
  const form = needsForm(menu);
  return (
    <div className="rounded-md border border-warning/30 bg-warning/10 px-3 py-2.5 text-sm">
      <div className="flex items-center gap-1.5 font-medium text-warning">
        <span className="inline-block h-1.5 w-1.5 animate-pulse rounded-full bg-warning" />
        {menu.title || "Waiting on you"}
        {age ? (
          <span className="ml-auto text-[11px] font-normal text-muted-foreground">{age}</span>
        ) : null}
      </div>
      {menu.body && !form ? (
        <pre className="mt-1.5 max-h-32 overflow-auto whitespace-pre-wrap break-all rounded bg-muted px-2 py-1.5 font-mono text-[12px] text-foreground-secondary">
          {menu.body}
        </pre>
      ) : null}
      {/* The form prints each question against its own options, so the single
          top-level question would be question 1 shown twice. */}
      {form ? null : <div className="mt-2 text-foreground">{menu.question}</div>}
      {form ? (
        <AnswerForm
          menu={menu}
          questions={menu.questions ?? []}
          busy={busy}
          onAnswer={onAnswer}
        />
      ) : optionless ? (
        <div className="mt-1.5 text-[12px] leading-snug text-muted-foreground">
          No options came with this one — open the session in emdash to see what is on
          screen. Cancelling clears it if nothing is, and the composer below still sends.
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
