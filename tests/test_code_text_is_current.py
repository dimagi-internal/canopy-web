"""Code comments and docstrings say what is true now, never what used to be.

Git keeps history (`git log -S`, `gh pr view`); a history line in a comment is a second copy that goes
wrong. A rule keeps its REASON in the present tense ("do not relax X: Y depends on it"); that is a rule,
not history. This scans Python comments and docstrings (stdlib `tokenize`/`ast`) and TypeScript `//` and
`/* */` comments for: an ISO date, a PR or issue number, a reference to the deleted `docs/superpowers` or
`docs/archive` specs, and "used to / formerly / was previously / until now" narrative. Pure stdlib.

Run `python tests/test_code_text_is_current.py` to list every hit with file:line.
"""
from __future__ import annotations

import ast
import io
import pathlib
import re
import tokenize

ROOT = pathlib.Path(__file__).resolve().parent.parent
PY_DIRS = ("apps", "runner", "config", "scripts", "tests", "sdk", "frontend/packages")
TS_DIRS = ("frontend/src", "frontend/packages")
SKIP_PARTS = {"node_modules", "dist", "migrations", ".venv", "__pycache__"}
# These name the patterns they forbid.
SKIP_FILES = {
    "tests/test_code_text_is_current.py",
    "tests/test_docs_current_state.py",
    "tests/test_route_docstrings_are_current.py",
}
HISTORY = re.compile(
    r"\b20\d\d-[01]\d-[0-3]\d\b"                                                # an ISO date
    r"|(?<![\w/&-])#\d{3,5}\b|\bPRs? #?\d+|\b[\w.-]+#\d{2,5}\b"                 # a PR or issue number
    r"|docs/superpowers|docs/archive"                                           # a deleted spec
    r"|\b(?:it|they|this|that|we|there|which) used to\b|\bused to be\b"                  # "it used to ..."
    r"|\b(?:formerly|was previously|until now|no longer (?:needed|used|required|necessary))\b",
    re.I,
)
# A string that LOOKS like history and is not: {(path, the matched text): why}. A stale entry fails.
ALLOW: dict[tuple[str, str], str] = {
    ("apps/harness/aws_iam_proof.py", "2011-06-15"): "the AWS STS API version sent in the request body",
    ("apps/host_grants/client_identity.py", "2026-07-28"): "an MCP protocol revision id",
    ("apps/mcp/project_gateway.py", "2026-07-28"): "an MCP protocol revision id",
    ("apps/mcp_apps_host/mcp_apps.py", "2026-01-26"): "the MCP Apps spec version, also PROTOCOL_VERSION",
    ("apps/reviews/schemas.py", "2026-10-07"): "an example run-id format",
    ("apps/runs/ddd.py", "2026-05-29"): "an example run-id format the code parses",
    ("apps/runs/tests/test_aggregate.py", "2026-07-14"): "a run-id example the test uses as data",
    ("apps/runs/timeline.py", "2026-06-02"): "an example run-id format the code parses",
    ("runner/canopy_runner/canopy_runner/inbox_rules.py", "2026-09-24"):
        "vendored byte-identical from canopy; tests/test_inbox_rules_vendored.py pins its sha256",
    ("runner/canopy_runner/canopy_runner/inbox_rules.py", "canopy#653"): "same vendored file",
    ("runner/canopy_runner/canopy_runner/inbox_rules.py", "canopy#679"): "same vendored file",
    ("tests/test_slack_window.py", "2026-11-01"): "the DST date the test asserts through its fixture",
    ("tests/test_session_ledger_fanout.py", "2026-08-27"):
        "left as is: touching this file runs tests that fail on main; clear it when they are fixed",
    ("sdk/python/src/canopy_sdk/resources.py", "2025-06-18"): "an MCP protocol revision id",
    ("frontend/src/api/tokenStatus.test.ts", "2027-03-01"): "the fixture date the test uses",
    ("frontend/src/components/ddd/DddLeftNav.tsx", "2026-06-02"): "an example run-id format the regex beside it parses",
}
# apps/*/api*.py and apps/contacts/hcp* route docstrings are published as OpenAPI text; their `#` comments are scanned.


def _files(dirs, suffixes):
    for d in dirs:
        base = ROOT / d
        if not base.exists():
            continue
        for p in sorted(base.rglob("*")):
            if p.suffix in suffixes and p.is_file() and not (SKIP_PARTS & set(p.parts)):
                rel = str(p.relative_to(ROOT))
                if rel not in SKIP_FILES and not rel.endswith(".d.ts") and "generated" not in p.name:
                    yield p, rel


def _route_docs_file(rel: str) -> bool:
    parts = rel.split("/")
    return rel.startswith("apps/") and (parts[-1].startswith("api") or parts[-1].startswith("hcp") or "/hcp" in rel)


def py_texts(path: pathlib.Path, rel: str):
    """Yield (line, text) for each comment and each docstring."""
    src = path.read_text(encoding="utf-8")
    try:
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            if tok.type == tokenize.COMMENT:
                yield tok.start[0], tok.string
    except (tokenize.TokenError, IndentationError, SyntaxError):
        pass
    if _route_docs_file(rel):
        return
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                yield body[0].lineno, body[0].value.value


def ts_texts(path: pathlib.Path, rel: str):
    """Yield (line, text) for each `//` and `/* */` comment, skipping strings, templates and regex-free code."""
    s = path.read_text(encoding="utf-8")
    i, n, line = 0, len(s), 1
    while i < n:
        c = s[i]
        if c == "\n":
            line += 1
            i += 1
        elif s.startswith("//", i):
            j = s.find("\n", i)
            j = n if j < 0 else j
            yield line, s[i:j]
            i = j
        elif s.startswith("/*", i):
            j = s.find("*/", i + 2)
            j = n if j < 0 else j + 2
            yield line, s[i:j]
            line += s.count("\n", i, j)
            i = j
        elif c in "\"'`":
            j = i + 1
            while j < n and s[j] != c:
                if s[j] == "\\":
                    j += 1
                elif c != "`" and s[j] == "\n":
                    break
                if j < n and s[j] == "\n":
                    line += 1
                j += 1
            i = j + 1
        else:
            i += 1


def hits():
    for p, rel in _files(PY_DIRS, {".py"}):
        for ln, text in py_texts(p, rel):
            for m in HISTORY.finditer(text):
                yield rel, ln, m.group(0), text.strip().splitlines()[0][:100]
    for p, rel in _files(TS_DIRS, {".ts", ".tsx"}):
        if "/src/" not in rel and not rel.startswith("frontend/src"):
            continue
        for ln, text in ts_texts(p, rel):
            for m in HISTORY.finditer(text):
                yield rel, ln, m.group(0), text.strip().splitlines()[0][:100]


def test_code_text_holds_no_history():
    bad = [f"{rel}:{n}: {text!r} in: {line}" for rel, n, text, line in hits() if (rel, text) not in ALLOW]
    assert not bad, (
        "Code comments and docstrings are current state only: state the rule as it holds now with its reason in "
        "the present tense, and put when and why it changed in the PR description. If a match is not history "
        "(a protocol revision id, a fixture), add (path, text) to ALLOW with the reason.\n" + "\n".join(bad[:60])
    )


def test_every_allow_entry_still_matches():
    seen = {(rel, text) for rel, _n, text, _line in hits()}
    stale = [k for k in ALLOW if k not in seen]
    assert not stale, f"ALLOW entries that no longer match anything (delete them): {stale}"


if __name__ == "__main__":
    import collections
    import sys

    cnt = collections.Counter()
    for rel, n, text, line in hits():
        cnt["/".join(rel.split("/")[:2] if rel.startswith(("apps/", "frontend/")) else rel.split("/")[:1])] += 1
        if "-v" in sys.argv:
            print(f"{rel}:{n}: {text!r}: {line}")
    for k, v in sorted(cnt.items()):
        print(v, k)
    print(sum(cnt.values()), "total")
