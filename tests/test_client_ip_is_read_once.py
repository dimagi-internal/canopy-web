"""Fitness test: `X-Forwarded-For` is read in ONE place.

Three hand-rolled parsers each took the header's FIRST entry — the one the
client writes — while the ALB appends the real address at the END. So any
caller could choose the key of a per-address rate limit and the address stamped
on an audit row. Each parser looked reasonable on its own; the defect was that
there were three. `apps/common/client_ip.py` is now the only reader, and this
test fails the build on a fourth.

Narrow on purpose: it looks for the header's NAME as a string constant
(`"HTTP_X_FORWARDED_FOR"`, `"x-forwarded-for"`), which is what reading it takes.
A comment or a docstring that mentions the header is not a read and is not
flagged.
"""
from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
HELPER = REPO_ROOT / "apps" / "common" / "client_ip.py"
SCAN_DIRS = ["apps", "config"]
NAMES = {"http_x_forwarded_for", "x-forwarded-for"}

#: Modules that NAME the header without parsing it, each with its reason.
ALLOWED = {
    # Carries the MCP client's header verbatim into the in-process request, so
    # the helper reads the ALB-appended value there too. Forwarding, not parsing.
    REPO_ROOT / "apps" / "mcp" / "api_tools.py",
}


def _reads(source: str) -> list[int]:
    hits = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.value.strip().lower() in NAMES:
                hits.append(node.lineno)
    return hits


def _production_files():
    for d in SCAN_DIRS:
        for path in sorted((REPO_ROOT / d).rglob("*.py")):
            parts = set(path.parts)
            if path == HELPER or path in ALLOWED or "migrations" in parts or "tests" in parts:
                continue
            if path.name.startswith("test_") or path.name == "tests.py":
                continue
            yield path


def test_only_the_helper_reads_x_forwarded_for():
    offenders = [
        f"{path.relative_to(REPO_ROOT)}:{line}"
        for path in _production_files()
        for line in _reads(path.read_text())
    ]
    assert not offenders, (
        "These read X-Forwarded-For outside apps/common/client_ip.py:\n  "
        + "\n  ".join(offenders)
        + "\n\nUse apps.common.client_ip.client_ip(request) (or from_scope for a "
        "socket). Its FIRST entry is whatever the client sent; only the helper "
        "knows which entry the ALB wrote."
    )


def test_the_detector_actually_detects():
    assert _reads('request.META.get("HTTP_X_FORWARDED_FOR")\n') == [1]
    assert _reads('headers.get("X-Forwarded-For")\n') == [1]
    assert _reads('# HTTP_X_FORWARDED_FOR in a comment\nx = 1\n') == []
    assert _reads('"""mentions HTTP_X_FORWARDED_FOR in prose"""\n') == []
