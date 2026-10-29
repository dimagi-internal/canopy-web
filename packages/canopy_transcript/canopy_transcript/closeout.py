"""The closeout verdict a turn ended on, read off its final message.

An unattended, incident-style turn (an alarm, a page) does not type its own "safe to close":
canopy's `canopy closeout record` computes a verdict from recorded facts and prints the status
line the turn ends on, tokened `[closeout: <VERDICT>]` (canopy `src/orchestrator/closeout.py`
owns the token; change both together). The verdict is CLOSED, MONITORING (agent-owned
re-checks scheduled), NEEDS_YOU (the line carries the person's one ask) or INCOMPLETE.

This module finds that line so a turn's result can say which it was — the field a session may
auto-close on (CLOSED only) and the one line NEEDS_YOU shows its owner. It judges nothing.
"""
from __future__ import annotations

import re

VERDICTS = ("CLOSED", "MONITORING", "NEEDS_YOU", "INCOMPLETE")
TOKEN = re.compile(r"\[closeout: (CLOSED|MONITORING|NEEDS_YOU|INCOMPLETE)\]")

#: The longest status line passed on whole. One line is the contract; this is a backstop.
MAX_LINE = 600


def find(text) -> tuple[str, str]:
    """(verdict, its status line) for the LAST closeout token in `text`; ("", "") if none."""
    if not isinstance(text, str) or not text:
        return "", ""
    found = None
    for m in TOKEN.finditer(text):
        found = m
    if found is None:
        return "", ""
    start = text.rfind("\n", 0, found.start()) + 1
    end = text.find("\n", found.end())
    line = text[start:end if end != -1 else len(text)].strip()
    return found.group(1), line[:MAX_LINE]
