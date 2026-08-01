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


@dataclass
class Option:
    number: int
    label: str


@dataclass
class Menu:
    question: str
    options: list[Option]
    title: str = ""
    body: str = ""
    selected: int | None = None
    raw: str = field(default="", repr=False)

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
        # Options are consecutive from 1; anything else is prose that happens to
        # be numbered.
        if not options and number != 1:
            continue
        if options and number != options[-1].number + 1:
            continue
        if not options:
            first_option_line = i
        options.append(Option(number, label))
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
    block = [l for l in lines[start:question_line] if l.strip()]
    title = block[0].strip() if block else ""
    body = _join_wrapped(block[1:]) if len(block) > 1 else ""

    return Menu(question=question, options=options, title=title, body=body,
                selected=selected, raw=text)


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
    """The keystrokes that answer a dialog.

    `None` means refuse, and sends Escape rather than a number — the one answer
    that is safe when the numbering is not what we think it is. Every dialog
    observed offers Esc.

    Roundtrip-verified: '1' + Enter on a real Bash-permission dialog ran the
    command.
    """
    if option is None:
        return ["\x1b"]
    return [str(option), "\r"]
