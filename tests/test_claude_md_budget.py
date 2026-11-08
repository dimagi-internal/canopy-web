"""CLAUDE.md is loaded into every session, so it stays a current-state index.

It holds what an agent needs before it knows what it is working on — how to
run, test and ship, and the invariants nothing may break — each as a rule plus
a pointer. Detail lives in docs/architecture/<topic>.md; history lives in the
PR description. Claude Code warns once a CLAUDE.md passes 150k characters.
Pure stdlib; no Django setup.
"""
from __future__ import annotations

import pathlib
import re

CLAUDE_MD = pathlib.Path(__file__).resolve().parent.parent / "CLAUDE.md"
CEILING = 60_000
# A date or a PR reference is history: what changed and when lives in GitHub.
HISTORY = re.compile(r"\b20\d\d-\d\d-\d\d\b|\(#\d+\)|\bPR #\d+|[\w-]+#\d{2,}")


def test_claude_md_stays_an_index():
    size = len(CLAUDE_MD.read_text(encoding="utf-8"))
    assert size <= CEILING, (
        f"CLAUDE.md is {size:,} chars (ceiling {CEILING:,}). Move subject detail into "
        "docs/architecture/<topic>.md and leave a one-line pointer; put history (what it "
        "replaced, what broke, when) in the PR description, not in docs."
    )


def test_claude_md_holds_no_history():
    hits = [
        f"  line {n}: {m.group(0)}"
        for n, line in enumerate(CLAUDE_MD.read_text(encoding="utf-8").splitlines(), 1)
        for m in HISTORY.finditer(line)
    ]
    assert not hits, (
        "CLAUDE.md is current state only, but it names a date or a PR:\n" + "\n".join(hits)
        + "\nState the rule as it holds now; put when and why it changed in the PR description."
    )


ARCH = CLAUDE_MD.parent / "docs" / "architecture"
TOPIC_DOC_CEILING = 50_000
# A topic doc that genuinely must stay whole: {"name.md": "why it cannot be split"}. Empty on purpose;
# every entry needs a reason, and a test below fails on an entry that no longer needs one.
TOPIC_DOC_ALLOWLIST: dict[str, str] = {}


def test_topic_docs_stay_readable_whole():
    over = {
        p.name: len(p.read_text(encoding="utf-8"))
        for p in sorted(ARCH.glob("*.md"))
        if len(p.read_text(encoding="utf-8")) > TOPIC_DOC_CEILING and p.name not in TOPIC_DOC_ALLOWLIST
    }
    assert not over, (
        "split by subject and index it; a topic doc nobody can read whole is not kept true. "
        f"Over {TOPIC_DOC_CEILING:,} chars: "
        + ", ".join(f"{name} ({size:,})" for name, size in over.items())
        + ". Move whole sections into docs/architecture/<parent>-<subject>.md (verbatim, one subject per file), "
        "list the new file in the parent's index and CLAUDE.md's topic index, then fix every `file.md#anchor` "
        "and `file.md § Heading` reference (tests/test_docs_links_resolve.py finds them)."
    )


def test_topic_doc_allowlist_has_reasons_and_is_needed():
    for name, reason in TOPIC_DOC_ALLOWLIST.items():
        assert reason.strip(), f"{name} is allowlisted without a reason"
        path = ARCH / name
        assert path.is_file() and len(path.read_text(encoding="utf-8")) > TOPIC_DOC_CEILING, (
            f"{name} no longer exceeds {TOPIC_DOC_CEILING:,} chars; remove it from TOPIC_DOC_ALLOWLIST"
        )


def test_every_topic_doc_is_named_by_an_index():
    """A split file nobody points at is an unread file: it is named in CLAUDE.md or in another topic doc."""
    docs = {p.name: p.read_text(encoding="utf-8") for p in ARCH.glob("*.md")}
    claude = CLAUDE_MD.read_text(encoding="utf-8")
    orphans = [
        name for name in sorted(docs)
        if name not in claude and not any(name in text for other, text in docs.items() if other != name)
    ]
    assert not orphans, (
        "split by subject and index it: " + ", ".join(orphans)
        + " is named by no index. List it in its parent doc's index or in CLAUDE.md's topic index."
    )
