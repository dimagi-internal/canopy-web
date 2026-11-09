"""Docs say what is true now, never what used to be.

Git keeps history (`git log -S`, `gh pr view`); a history line in a doc is a second copy that goes
wrong. This fails on the shapes a history line takes in CLAUDE.md, the root `*.md` and every
`docs/**/*.md`, and on the return of the design-record directories. A rule keeps its REASON in the
present tense ("do not relax X: Y depends on it"); that is a rule, not history. Pure stdlib.
"""
from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent
DOCS = [ROOT / "CLAUDE.md", *sorted(ROOT.glob("*.md")), *sorted((ROOT / "docs").rglob("*.md"))]
HISTORY = re.compile(
    r"\b20\d\d-[01]\d-[0-3]\d\b"                               # an ISO date
    r"|(?<![\w/&-])#\d{3,5}\b|\bPR #?\d+"                      # a PR or issue number
    r"|\b(?:used to|formerly|was previously|owner decision|owner's rule|until now)\b"
    r"|docs/superpowers|docs/archive",
    re.I,
)
# A string that LOOKS like history and is not: {(doc, the matched text): why}. A stale entry fails.
ALLOW: dict[tuple[str, str], str] = {
    ("docs/architecture/embedding-actions-and-access.md", "2025-06-18"): "an MCP protocol revision id the host must send",
    ("docs/architecture/work.md", "2026-07-28"): "an MCP protocol revision id: the revision with no per-connection session",
}
# The note that states the rule names the patterns it forbids.
RULE_NOTE = "> **How to write this file.**"
HISTORY_DIRS = ("docs/superpowers", "docs/archive", "docs/case-studies")


def _hits():
    for doc in DOCS:
        rel = str(doc.relative_to(ROOT))
        for n, line in enumerate(doc.read_text(encoding="utf-8").splitlines(), 1):
            if line.startswith(RULE_NOTE):
                continue
            for m in HISTORY.finditer(line):
                yield rel, n, m.group(0), line.strip()[:100]


def test_docs_hold_no_history():
    bad = [f"{rel}:{n}: {text!r} in: {line}" for rel, n, text, line in _hits() if (rel, text) not in ALLOW]
    assert not bad, (
        "Docs are current state only: state the rule as it holds now and put when and why it changed in the "
        "PR description. If a match is not history (a protocol revision id), add it to ALLOW with the reason.\n"
        + "\n".join(bad)
    )


def test_every_allow_entry_still_matches():
    seen = {(rel, text) for rel, _n, text, _line in _hits()}
    stale = [k for k in ALLOW if k not in seen]
    assert not stale, f"ALLOW entries that no longer match anything (delete them): {stale}"


def test_design_record_directories_do_not_return():
    back = [d for d in HISTORY_DIRS if (ROOT / d).exists()]
    assert not back, f"{back}: history lives in git, not in docs (decisions stay as present-tense rules in topic docs)"
