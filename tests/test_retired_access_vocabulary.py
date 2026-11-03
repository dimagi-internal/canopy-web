"""The agent-access vocabulary that was retired must not come back in the docs people copy from.

An agent's capability used to carry `role: viewer|user`, and access-list lines were `viewer|user|editor`.
Today lines are `read_only|read_write|editor` (or name one capability) and a capability carries no
role (`apps/agents/roles.py`, `docs/architecture/access.md`). A doc that still shows the old words
teaches an author a 422. The interface keys `full:`, `callers:` and `editors:` are refused the same way
(`apps/agents/interface.py::parse`), so a YAML example using one is guarded too.

Guarded: the root docs, `docs/architecture`, the SDK and package READMEs, the runner READMEs, the
templates, and the runner's own comments (a comment naming the `full:` rule teaches the old model).
"""
import re

import pytest

from tests._source import ROOT

pytestmark = pytest.mark.guard

# Each pattern: (regex, files it must not appear in, or None for all guarded files). A pattern with a
# legitimate meaning in some file lists that file in ALLOW (viewer = a View/presence word, say).
RETIRED = [
    re.compile(r"owner > admin > editor > user > viewer"),
    re.compile(r"role:\s*viewer\b"),
    re.compile(r"viewer\|user"),
    re.compile(r"`viewer` or `user`"),
    # A retired interface key used as a YAML key (not a backticked mention of a refused key).
    re.compile(r"^\s*(?:full|callers|editors):\s*(?:$|\[|-|\{)"),
    # A comment describing an access decision as a `full:` rule.
    re.compile(r"`full:` rule"),
]
# rel path -> patterns (by .pattern) that file may legitimately contain
ALLOW: dict[str, set[str]] = {}

_DOC_GLOBS = [
    "docs/architecture/*.md",
    "sdk/python/README.md",
    "runner/**/README*.md",
    "packages/*/README*",
    "templates/**/*",
]
_CODE_GLOBS = ["runner/**/*.py"]


def _guarded() -> list[str]:
    out = ["CLAUDE.md", "ARCHITECTURE.md", "README.md"]
    for g in (*_DOC_GLOBS, *_CODE_GLOBS):
        out += [str(p.relative_to(ROOT)) for p in ROOT.glob(g)
                if p.is_file() and "node_modules" not in p.parts and "tests" not in p.parts]
    return sorted(set(out))


def test_no_retired_agent_role_words_in_docs():
    hits = []
    for rel in _guarded():
        allowed = ALLOW.get(rel, set())
        try:
            lines = (ROOT / rel).read_text().splitlines()
        except UnicodeDecodeError:
            continue
        for n, line in enumerate(lines, 1):
            if any(p.search(line) and p.pattern not in allowed for p in RETIRED):
                hits.append(f"{rel}:{n}: {line.strip()[:100]}")
    assert not hits, "retired agent-access words (use read_only|read_write|editor):\n" + "\n".join(hits)
