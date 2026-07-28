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

    # A dialog always offers a way out, and prose never does. Requiring that
    # footer BELOW the options is what separates a real menu from an agent
    # writing "1. Read the file / 2. Change the thing" — which parsed as a menu
    # until this check existed. Numbered lines are simply not evidence: they are
    # the most common shape in an agent's own output.
    #
    # Fails closed on purpose. A dialog with no footer would be missed (the
    # phone shows "needs you" with no buttons, which the terminal can still
    # answer); a false positive tells you an agent is blocked while it works,
    # and a few of those and the signal is worthless.
    if not any(_FOOTER.search(line) for line in lines[first_option_line:]):
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
