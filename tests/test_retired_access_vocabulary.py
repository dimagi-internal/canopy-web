"""The agent-access vocabulary that was retired must not come back in the docs people copy from.

An agent's capability used to carry `role: viewer|user`, and access-list lines were `viewer|user|editor`.
Today lines are `read_only|read_write|editor` (or name one capability) and a capability carries no
role (`apps/agents/roles.py`, `docs/architecture/access.md`). A doc that still shows the old words
teaches an author a 422.
"""
import re

from tests._source import ROOT

RETIRED = [
    re.compile(r"owner > admin > editor > user > viewer"),
    re.compile(r"role:\s*viewer\b"),
    re.compile(r"viewer\|user"),
    re.compile(r"`viewer` or `user`"),
]
DOCS = (ROOT / "docs" / "architecture").glob("*.md")
GUARDED = ["CLAUDE.md", "ARCHITECTURE.md", *(str(p.relative_to(ROOT)) for p in DOCS)]


def test_no_retired_agent_role_words_in_docs():
    hits = []
    for rel in GUARDED:
        for n, line in enumerate((ROOT / rel).read_text().splitlines(), 1):
            if any(p.search(line) for p in RETIRED):
                hits.append(f"{rel}:{n}: {line.strip()[:100]}")
    assert not hits, "retired agent-access words (use read_only|read_write|editor):\n" + "\n".join(hits)
