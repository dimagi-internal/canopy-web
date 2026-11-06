"""A site an agent is offered on is a "host site"; a person's signed-in MCP client is an "authorized app".

"Connected site" and "connected app" were both words for the first, and "connected app" also for the
second, while "connection" is a third-party OAuth login (`apps/connections`) and Connect is a product.
They were retired on 2026-11-06. History keeps them: migrations, dated specs and plans, changelogs.
Salesforce's own "Connected App" (a Salesforce object) is not ours to rename.
"""
import re

import pytest

from tests._source import ROOT

pytestmark = pytest.mark.guard

RETIRED = re.compile(r"connected[ _-]?(site|app)|connectedsite|connectedapp|\bconnect_app\b", re.IGNORECASE)

_GLOBS = ["apps/**/*.py", "frontend/src/**/*.ts", "frontend/src/**/*.tsx", "frontend/packages/*/src/**/*.ts",
          "frontend/packages/*/src/**/*.tsx", "sdk/python/src/**/*.py", "sdk/python/README.md", "runner/**/*.py",
          "docs/architecture/*.md", "docs/runbooks/*.md", "CLAUDE.md", "ARCHITECTURE.md", "README.md"]
_SKIP = re.compile(r"/migrations/|^apps/connections/|generated\.ts$|node_modules")
#: Lines that name the retired words on purpose, or Salesforce's.
_ALLOWED = re.compile(r"retired|Salesforce|ConnectedApplication|Connected App")


def test_no_connected_site_or_connected_app():
    hits = []
    for g in _GLOBS:
        for p in ROOT.glob(g):
            rel = str(p.relative_to(ROOT))
            if not p.is_file() or _SKIP.search(rel):
                continue
            for n, line in enumerate(p.read_text(errors="ignore").splitlines(), 1):
                if RETIRED.search(line) and not _ALLOWED.search(line):
                    hits.append(f"{rel}:{n}: {line.strip()[:120]}")
    assert not hits, 'say "host site" (or "authorized app" for an MCP client):\n' + "\n".join(hits)
