"""Read a Claude Code dialog out of a rendered terminal, and answer it.

**Why this exists.** A hook cannot describe a menu. `Notification` fires when
Claude Code wants a human and carries a message string — not the options, not
the command being approved, and no way to reply. ACP *does* carry all of it
(`session/request_permission` gives labelled options plus the toolCall), but
that is the cloud path: on a laptop emdash owns the session, so the only place
the menu exists is the terminal it is drawn in.

**Why a rendered grid, not the PTY stream.** Claude Code draws spaces as
cursor-forward escapes (`ESC[1C`), so stripping ANSI from the raw stream welds
words together (`Accessingworkspace:`). xterm resolves those into cells, and the
runner reads the resulting text out of emdash's DOM over CDP — so this parser
takes the grid, which is what a terminal shows.

Verified end to end on 2026-07-28: a real dialog was captured from a live
`claude`, parsed by this module, answered with `answer_keys`, and the file it
asked about was actually deleted.

**It fails closed.** `find_menu` returns None unless it sees consecutively
numbered options AND a footer offering a way out. A false positive tells a phone
an agent is blocked when it is working, and after a few of those nobody trusts
the signal.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

# The pointer marking the highlighted row. Cosmetic for answering (we send the
# NUMBER) but shown to the user, so it must be stripped from labels.
CURSOR = "❯"

# "1. Yes" / "❯ 2. Yes, and always allow …" — leading indent varies with the box.
_OPTION = re.compile(rf"^\s*(?:{CURSOR}\s*)?(\d+)\.\s+(\S.*)$")

# Every dialog observed ends its prose with a question. Requiring one is what
# keeps a numbered LIST an agent wrote from parsing as a menu.
_QUESTION = re.compile(r"^\s*(.*\?)\s*$")

# Footer hints, never options or subject.
_FOOTER = re.compile(r"(Esc to cancel|Enter to confirm|Tab to amend|ctrl\+e to explain)")

# The rule drawn above a dialog. Marks where the subject block starts.
_RULE = re.compile(r"^[─━-]{10,}$")

# How far short of the frame width a word-wrapped line may stop. Wrapping breaks
# at a space, so the break lands wherever the next word began — a few characters
# of slack, not a full word, because the wider this is the more readily a short
# line of SUBJECT gets swallowed into the question above the options.
_WRAP_SLACK = 4


# A multi-select row: "[ ] Red" / "[✔] Red". The TUI draws these ONLY for a
# multiSelect question, which is what makes them the reliable on-screen
# discriminator — the tool input's `multiSelect` flag is not visible here.
_CHECKBOX = re.compile(r"^\[([ ✔xX])\]\s*(.*)$")

# The tab strip a multi-question (or any multi-select) ask draws above the
# question: "←  ☒ Colors  ☐ Size  ✔ Submit  →". A single-question single-select
# draws a bare "☐ Size" with no arrows and no Submit tab, and needs no submit
# step — which is exactly why today's number+Enter works there and nowhere else.
_TAB_MARK = re.compile(r"[☐☒✔]")

# The final tab. Reached with Tab, and a plain numbered menu once there, so the
# ordinary answer path presses its button.
_REVIEW = "Ready to submit your answers?"
SUBMIT_TAB = "Submit"


@dataclass
class Option:
    number: int
    label: str
    #: True/False on a multi-select row, None when the row has no checkbox.
    checked: bool | None = None


@dataclass
class Menu:
    question: str
    options: list[Option]
    title: str = ""
    body: str = ""
    selected: int | None = None
    #: Tab labels in drawn order, e.g. ["Colors", "Size", "Submit"]. Empty when
    #: the dialog draws no tab strip (a permission prompt, a trust gate).
    tabs: list[str] = field(default_factory=list)
    raw: str = field(default="", repr=False)

    @property
    def is_review(self) -> bool:
        """The Submit tab, showing the answers back before they are sent."""
        return _REVIEW in self.raw

    @property
    def is_multi_select(self) -> bool:
        """Whether a number key TOGGLES here rather than answering.

        Read off the drawn checkboxes rather than the tool input: this is the
        screen the keystroke actually lands on, and the runner's whole safety
        story is that it verifies against the screen before pressing.
        """
        return any(o.checked is not None for o in self.options)

    @property
    def needs_submit(self) -> bool:
        """Whether answering every question still leaves a Submit to press.

        A single-question single-select draws no Submit tab and completes on the
        number alone. Everything else has to be walked to the review screen.
        """
        return SUBMIT_TAB in self.tabs

    def checked_numbers(self) -> list[int]:
        return [o.number for o in self.options if o.checked]

    def allows(self, number: int | None) -> bool:
        """Whether `number` is actually on this menu.

        A client can post any integer. Typing '9' at a three-option dialog
        answers nothing, leaves the prompt open and wedges the turn — so the
        runner checks before pressing a key.
        """
        if number is None:
            return True          # refusing (Esc) is always available
        return any(o.number == number for o in self.options)


def _join_wrapped(lines: list[str]) -> str:
    """Rejoin a command the TUI hard-wrapped across grid lines.

    It wraps mid-token (`…canopy-web-emd` / `ash-acp-runner…`), so joining with
    a space or newline corrupts the very string a human is reading in order to
    decide. Continuation lines are concatenated with nothing; a line that starts
    a new sentence-ish fragment keeps its space.
    """
    out = ""
    for line in lines:
        piece = line.strip()
        if not piece:
            continue
        if not out:
            out = piece
        elif out.endswith(("&&", "|", "&", ";")) or piece.startswith(("&&", "|", "-")):
            out += " " + piece
        elif out[-1:].isspace():
            out += piece
        else:
            # Mid-token wrap: the grid split a path or flag, so no separator.
            out += piece
    return out


def _parse_tabs(lines: list[str], first_option_line: int) -> tuple[list[str], int | None]:
    """The tab strip's labels, and the line it was drawn on.

    "←  ☒ Colors  ☐ Size  ✔ Submit  →" -> (["Colors", "Size", "Submit"], i).
    A single-question single-select draws a bare "☐ Size"; that parses to
    ["Size"] with no "Submit", which is precisely the distinction the driver
    needs — no Submit tab means the number key finishes the dialog by itself.

    Searched only ABOVE the options: a checked multi-select row ("[✔] Red")
    carries the same glyph and would otherwise parse as a tab strip.

    Backwards from the options, so the NEAREST strip wins. A terminal holds the
    whole scrollback, and an agent that printed a ✔ earlier in the session must
    not be mistaken for the tab strip of the dialog now on screen.
    """
    for i in range(first_option_line - 1, -1, -1):
        line = lines[i]
        if not _TAB_MARK.search(line) or _OPTION.match(line):
            continue
        stripped = line.strip().strip("←→").strip()
        labels = [p.strip() for p in _TAB_MARK.split(stripped) if p.strip()]
        if labels:
            return labels, i
    return [], None


def _dialog_runs_to_the_bottom(lines: list[str], selected: int | None) -> bool:
    """Whether this looks like a live dialog whose footer fell off the frame.

    Two conditions, and dropping either one lets prose through:

    `selected is not None` — the TUI painted its selection cursor on an option.
    An agent writing a numbered list does not draw "❯" at the head of one.

    The last drawn line is an option — the dialog is the bottom of the screen.
    Claude Code draws a dialog where the composer would be, so nothing follows a
    live one. A menu an agent *quoted* (this very session pasted one into a
    report) has prose or a composer beneath it and is rejected here.
    """
    if selected is None:
        return False
    for line in reversed(lines):
        if not line.strip():
            continue
        return _OPTION.match(line) is not None
    return False


def _unwrap_question(lines: list[str], question: str, question_line: int):
    """Reattach the earlier grid lines a long question was word-wrapped across.

    The question is found as the last line ending in '?', which on a wrapped one
    is only its TAIL: a real dialog reached the phone reading "press into that
    session?" — grammatical, useless, and indistinguishable from a short question
    (captured 2026-08-01).

    Joined only on the hard-wrap signature: the line above ran to the frame's own
    width and broke at a space. That is deliberately narrow. A dialog's SUBJECT
    also sits directly above its question — the permission prompt's "Delete
    target.txt and verify" above "Do you want to proceed?" — and those are two
    different things; swallowing one into the other would misreport what is being
    asked, which is worse than a truncated question.
    """
    width = max((len(l) for l in lines), default=0)
    if width <= 0:
        return question, question_line
    start = question_line
    while start > 0:
        above = lines[start - 1]
        if not above.strip() or _RULE.match(above.strip()):
            break
        if not above.endswith(" ") or len(above) < width - _WRAP_SLACK:
            break
        start -= 1
    if start == question_line:
        return question, question_line
    return " ".join(l.strip() for l in lines[start:question_line + 1]), start


def find_menu(text: str) -> Menu | None:
    """The dialog on this screen, or None.

    Deliberately strict — see the module docstring on failing closed.
    """
    if not text:
        return None
    lines = text.splitlines()

    # Options first: they are the least ambiguous thing on the screen.
    options: list[Option] = []
    selected: int | None = None
    first_option_line = None
    for i, line in enumerate(lines):
        m = _OPTION.match(line)
        if not m:
            continue
        number, label = int(m.group(1)), m.group(2).strip()
        if _FOOTER.search(label):
            continue
        # "[ ] Red" -> checked=False, label="Red". Kept off the label because the
        # label is what a phone renders as a button, and "[ ] Red" reads as a
        # broken string there while the box state belongs in its own field.
        checked = None
        box = _CHECKBOX.match(label)
        if box:
            checked = box.group(1) != " "
            label = box.group(2).strip()
        # Options are consecutive from 1; anything else is prose that happens to
        # be numbered.
        if not options and number != 1:
            continue
        if options and number != options[-1].number + 1:
            continue
        if not options:
            first_option_line = i
        options.append(Option(number, label, checked))
        if CURSOR in line:
            selected = number

    if len(options) < 2 or first_option_line is None:
        return None

    # A dialog offers a way out, and prose never does. Requiring that footer
    # BELOW the options is what separates a real menu from an agent writing
    # "1. Read the file / 2. Change the thing" — which parsed as a menu until
    # this check existed. Numbered lines are simply not evidence: they are the
    # most common shape in an agent's own output.
    #
    # But the footer is only drawn if there is a ROW LEFT TO DRAW IT ON. A tall
    # AskUserQuestion — a long question, five or six options, a description
    # under each — overflows a short emdash pane, and the line that falls off the
    # bottom is the footer. Captured live 2026-08-01 from two sessions blocked
    # this way, one for 15 minutes and one for 1h20m, both showing the phone
    # nothing: 41-row frames ending mid-dialog on "6. Chat about this", no
    # footer anywhere. The docstring predicted the miss ("a dialog with no
    # footer would be missed"); this is that case, and it is not rare.
    #
    # So a second, equally strict acceptance: the TUI's own selection cursor
    # sits on one of the options AND the dialog runs to the bottom of the frame.
    # Both halves matter. Prose does not draw "❯" at the head of a numbered
    # line, and a live dialog is always the LAST thing on the screen — the TUI
    # draws it where the composer would be — so a menu an agent merely quoted
    # has its own output below it and is still rejected.
    if not any(_FOOTER.search(line) for line in lines[first_option_line:]) \
            and not _dialog_runs_to_the_bottom(lines, selected):
        return None

    # The question is the last line ending in '?' ABOVE the options.
    question = ""
    question_line = None
    for i in range(first_option_line - 1, -1, -1):
        m = _QUESTION.match(lines[i])
        if m and m.group(1).strip():
            question, question_line = m.group(1).strip(), i
            break
    if not question:
        # The trust gate states rather than asks on its final line, so fall back
        # to the nearest prose line above the options.
        for i in range(first_option_line - 1, -1, -1):
            if lines[i].strip() and not _RULE.match(lines[i].strip()):
                question, question_line = lines[i].strip(), i
                break
    if not question:
        return None
    question, question_line = _unwrap_question(lines, question, question_line)

    # Subject: everything between the rule above and the question. First line is
    # the title ("Bash command"), the rest is the body (the command itself).
    start = 0
    for i in range(question_line - 1, -1, -1):
        if _RULE.match(lines[i].strip()):
            start = i + 1
            break
    tabs, tab_line = _parse_tabs(lines, first_option_line)

    # The tab strip sits inside the subject block and is NOT subject: rendered as
    # a title it reaches a phone as "← ☒ Close July ☒ Aug goals ✔ Submit →",
    # which is chrome from a terminal nobody is looking at. It has its own field
    # now, so drop it here.
    block = [lines[i].strip() for i in range(start, question_line)
             if lines[i].strip() and i != tab_line]
    title = block[0] if block else ""
    if not title:
        # The tab strip was the whole subject block, which is the normal shape of
        # an AskUserQuestion — it draws its header there and nothing else. With
        # one question that header IS the dialog's label, so keep using it (minus
        # the ☐ chrome, which meant nothing away from the terminal). With several
        # we cannot tell which tab is current from the strip alone, and a label
        # naming the wrong question is worse than none: the client has the real
        # per-question headers in `questions`.
        answerable = [t for t in tabs if t != SUBMIT_TAB]
        title = answerable[0] if len(answerable) == 1 else ""
    body = _join_wrapped(block[1:]) if len(block) > 1 else ""

    return Menu(question=question, options=options, title=title, body=body,
                selected=selected, tabs=tabs, raw=text)


def find_menu_settled(read_screen, *, attempts: int = 3, delay: float = 0.8):
    """`find_menu`, tolerant of a half-drawn frame.

    Observed live 2026-07-28: a single read caught the TUI mid-render — the
    options were on screen but the footer had not been painted yet — so the
    dialog did not parse, and the answer was dropped as stale. Failing safe is
    right, but a phone tap that silently does nothing is its own bug, and the
    frame settles in well under a second.

    Takes a callable rather than a screen so the retry actually re-reads.
    """
    found = None
    for attempt in range(attempts):
        found = find_menu(read_screen())
        if found is not None:
            return found
        if attempt < attempts - 1:
            time.sleep(delay)
    return found


def answer_keys(option: int | None) -> list[str]:
    """The keystrokes that answer a SINGLE-select dialog.

    `None` means refuse, and sends Escape rather than a number — the one answer
    that is safe when the numbering is not what we think it is. Every dialog
    observed offers Esc.

    Roundtrip-verified: '1' + Enter on a real Bash-permission dialog ran the
    command.

    NOT sufficient for a multi-select or a multi-question ask: there the number
    key toggles a checkbox and the trailing Enter answers nothing, so the dialog
    stays up having silently changed state. `plan_step` drives those.
    """
    if option is None:
        return ["\x1b"]
    return [str(option), "\r"]


TAB = "\t"
SUBMIT_ANSWERS = "Submit answers"

# An entry in a keystroke list that means TYPE this, rather than press it. The
# transport tells the two apart by this prefix: a pressed key is one character
# (or a control character named by the sidecar), and free text is neither.
TEXT_PREFIX = "text:"

# The option Claude Code appends to every question for an answer that is not on
# the menu. Its NUMBER is assigned by the TUI, not by the tool input, so it can
# only be read off the rendered screen — which is why the driver looks for it by
# label rather than computing len(declared) + 1.
TYPE_SOMETHING = "type something"


def type_something_number(menu: "Menu") -> int | None:
    """The "Type something" row's number on this screen, or None once it has
    been filled in — the TUI replaces the label with the text you typed."""
    for option in menu.options:
        if option.label.rstrip(".").casefold() == TYPE_SOMETHING:
            return option.number
    return None


DOWN = "\x1b[B"


def _free_text_step(menu: "Menu", text: str) -> list[str] | None:
    """Keystrokes to put `text` into this question, or [] if it is already there.

    None means we cannot: the row is gone and nothing on screen carries the text,
    so typing would land somewhere unpredictable. Refusing costs the answer and
    says so; guessing types a sentence into whatever has focus.

    Typing goes to whatever row the CURSOR is on, so the cursor has to be walked
    there with arrow keys. A number key will not do it on a multi-select: there a
    number TOGGLES a checkbox and leaves the cursor where it was, so the text
    lands on the wrong row and the tick is spurious. Observed live — pressing the
    row's number, typing and pressing Enter left the label reading "Type
    something", un-ticked the box the previous step had ticked, and the driver
    oscillated until it hit its step cap.

    Enter COMMITS the text, and on a tabbed ask also advances to the next tab.
    It is not optional: while the row is being edited the dialog swallows Tab
    entirely, so without it the driver pressed Tab at a screen that could not
    respond and stopped (correctly) on its no-progress guard, answer unsent.
    Measured live — Tab alone left the screen byte-identical, Enter moved it on.

    Removed once, for the right reason and the wrong case: an Enter that follows
    a NUMBER press cleared the earlier pick, because the number had toggled the
    wrong row. Following a cursor-walk it is correct and required.
    """
    number = type_something_number(menu)
    if number is None:
        entered = any(option.label.strip() == text.strip() for option in menu.options)
        return [] if entered else None
    walk = [DOWN] * max(0, number - (menu.selected or 1))
    # How the row is COMMITTED differs by mode, because what sits below it does.
    # On a multi-select the next row is the dialog's own Next/Submit action, so ↓
    # steps off the field, banks the text (the box stays ticked) and lands the
    # cursor somewhere the grid can plainly show — no hidden edit state left to
    # guess at. On a single-select the row below is "Chat about this", so ↓ there
    # would park the cursor on something that opens a chat; Enter is the verified
    # commit for that shape (it produced `GOT=Medium` from a real agent).
    commit = DOWN if menu.is_multi_select else "\r"
    return walk + [TEXT_PREFIX + text, commit]

# Bound on the drive loop. Each question costs at most (toggles + one Tab), so
# this is generous for any real ask while still terminating if the screen stops
# responding the way we read it — a loop pressing keys into a terminal forever
# is the one failure mode worse than a dropped answer.
MAX_STEPS = 60


def normalise(text: str) -> str:
    return " ".join((text or "").split()).casefold()


def question_index(menu: "Menu", questions: list[dict]) -> int | None:
    """Which declared question the screen is currently showing, or None.

    Matched on the question TEXT rather than the tab strip, because the strip
    marks which tabs are answered but not which one you are on — the current tab
    is distinguished by colour, and colour does not survive the read.

    Prefix-tolerant: a long question is word-wrapped and can be rejoined with
    slightly different spacing than the tool input carried.
    """
    shown = normalise(menu.question)
    if not shown:
        return None
    best = None
    for q in questions:
        want = normalise(q.get("question") or q.get("header") or "")
        if not want:
            continue
        if want == shown:
            return q.get("index", questions.index(q))
        if (want.startswith(shown) or shown.startswith(want)) and len(shown) >= 12:
            # Longest match wins: two questions can share an opening clause.
            if best is None or len(want) > best[1]:
                best = (q.get("index", questions.index(q)), len(want))
    return best[0] if best else None


def screen_state(menu: "Menu") -> tuple:
    """What this screen IS, for spotting a step that changed nothing.

    Question plus every row's number, label and tick — the only things the driver
    reacts to, so two screens equal under this are two screens it would treat
    identically and therefore answer identically. Comparing raw text instead
    would make a spinner or a clock look like progress.
    """
    return (
        menu.question,
        menu.is_review,
        tuple((o.number, o.label, o.checked) for o in menu.options),
    )


def plan_step(menu: "Menu", questions: list[dict], selections: list[list[int]],
              texts: list[str] | None = None) -> list[str] | None:
    """The NEXT keystrokes for this screen, or None when there is nothing left.

    Deliberately one step at a time, re-reading between steps, rather than one
    computed key program. Three reasons, all observed on a real TUI:

    * A checkbox press is a TOGGLE, so a replayed answer un-answers it. Driving
      from the drawn state makes the whole operation idempotent — which is what
      makes the double-delivery this system already has (WS frame plus poll
      tick) harmless instead of destructive.
    * A single-select question AUTO-ADVANCES to the next tab when answered, and
      a multi-select does not, so the tab you are on after a keypress is not
      something a blind program can know.
    * The dialog can change under us. Every step re-verifies before pressing,
      which is the existing safety property, kept.

    Returns [] to mean "press nothing, but we are not done" — never used, but
    the distinction from None matters to the caller's loop.
    """
    if menu.is_review:
        for option in menu.options:
            if normalise(option.label).startswith(normalise(SUBMIT_ANSWERS)):
                return [str(option.number), "\r"]
        return None  # a review screen we do not recognise: refuse to guess

    index = question_index(menu, questions)
    if index is None or index >= len(selections):
        return None
    want = sorted(selections[index])
    text = (texts[index] if texts and index < len(texts) else "") or ""

    # The TUI appends its own rows ("Type something", "Chat about this") AFTER
    # the declared options, and `selections` only ever numbers the declared ones.
    # So anything past that count is not ours to toggle — and once free text has
    # been entered its row is CHECKED, so a diff that considered it would untick
    # it and erase what was typed.
    declared = len(questions[index].get("options") or []) if index < len(questions) else 0

    if menu.is_multi_select:
        # Toggle only the DIFFERENCES, so re-running this against a screen that
        # already matches presses nothing at all.
        have = set(menu.checked_numbers())
        differing = [n for n in menu.options
                     if (not declared or n.number <= declared)
                     and (n.number in want) != (n.number in have)]
        if differing:
            return [str(o.number) for o in differing]
        if text:
            # After the boxes, never before: the cursor ends up on the text row,
            # and a number pressed from there would toggle the wrong thing.
            step = _free_text_step(menu, text)
            if step is None:
                return None
            # [] means the text is already in and committed; this tab is done.
            return step or ([TAB] if menu.needs_submit else None)
        # This tab is right; move on. Tab clamps at the review screen rather
        # than wrapping, so over-pressing it cannot walk us back round.
        return [TAB] if menu.needs_submit else None

    if text:
        # Single-select: free text REPLACES the pick — the TUI moves the
        # selection onto the text row, so a declared option pressed as well
        # would simply overwrite it.
        step = _free_text_step(menu, text)
        if step is None:
            return None
        # Committed already; nothing left for this question.
        return step or ([TAB] if menu.needs_submit else None)

    # Single-select: pressing the number both answers and advances.
    if not want:
        return [TAB] if menu.needs_submit else None
    number = want[0]
    if not menu.allows(number):
        return None
    return [str(number)] if menu.needs_submit else [str(number), "\r"]
