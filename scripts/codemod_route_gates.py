"""One-shot codemod: move apps/api/route_gates.py's per-operation tables onto the views.

Reads GATES, VIEWER_MAY_MUTATE, SETTINGS and NOT_SETTINGS (and the comments
beside each entry), finds the view behind each operationId, and inserts
``@gate(...)`` directly above its ``def``, plus the import. Idempotent: a view
that already declares a gate is skipped.

    DJANGO_SETTINGS_MODULE=config.settings.test uv run python scripts/codemod_route_gates.py
"""
from __future__ import annotations

import ast
import inspect
import io
import os
import re
import subprocess
import sys
import textwrap
import tokenize
from collections import defaultdict

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.test")
django.setup()

from apps.api import route_gates as rg  # noqa: E402
from apps.api.api import api  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GATES_PY = os.path.join(ROOT, "apps/api/route_gates.py")
MAX = 120
IMPORT = "from apps.api.route_gates import gate"


# ---- comments beside each table entry ----

def _comment_tokens(src: str) -> dict[int, tuple[str, bool]]:
    """line -> (comment text without '#', alone on its line)."""
    out = {}
    lines = src.splitlines()
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type == tokenize.COMMENT:
            line = tok.start[0]
            alone = not lines[line - 1][: tok.start[1]].strip()
            out[line] = (tok.string.lstrip("#").strip(), alone)
    return out


def _table_comments(src: str, name: str, is_set: bool) -> dict[str, list[str]]:
    lines = src.splitlines()
    comments = _comment_tokens(src)
    start = next(i for i, ln in enumerate(lines, 1) if ln.startswith(f"{name}:"))
    out: dict[str, list[str]] = defaultdict(list)
    pending: list[str] = []
    keys_seen: list[str] = []
    for n in range(start + 1, len(lines) + 1):
        raw = lines[n - 1]
        if raw.startswith("}"):
            break
        text, alone = comments.get(n, ("", False))
        if alone:
            pending.append(text)
            continue
        code = raw
        if n in comments:
            code = raw[: _comment_col(raw, comments[n][0])]
        keys = re.findall(r'"([^"]+)"' if is_set else r'"([^"]+)":', code)
        if not keys:
            continue
        keys_seen.extend(keys)
        lead = [c for c in pending if _keep_lead(c, name)]
        pending = []
        for k in keys:
            out[k].extend(lead)
            if text:
                out[k].append(text)
    return out, keys_seen


def _comment_col(raw: str, text: str) -> int:
    idx = raw.rfind("# " + text) if text else raw.rfind("#")
    return idx if idx >= 0 else len(raw)


def _keep_lead(comment: str, table: str) -> bool:
    if not comment or comment.startswith("---"):
        return False
    if table == "SETTINGS":
        return False  # group headings ("agents: routing ..."); setting=True says it
    if table == "NOT_SETTINGS" and comment in rg.WRITE_KINDS:
        return False  # "# content" etc: the kind= says it
    return True


# ---- the views ----

def _views() -> dict[str, object]:
    api.get_openapi_schema()
    out = {}
    for bound in api._get_bound_routers():
        for pv in bound.path_operations.values():
            for op in pv.operations:
                oid = op.operation_id or api.get_openapi_operation_id(op)
                out[oid] = inspect.unwrap(op.view_func)
    return out


def _render(labels, setting, kind, viewer_write, indent: str) -> list[str]:
    args = [repr_str(lb) for lb in labels]
    if setting:
        args.append("setting=True")
    if kind:
        args.append(f"kind={repr_str(kind)}")
    if viewer_write is None:
        line = f"{indent}@gate({', '.join(args)})"
        if len(line) <= MAX:
            return [line]
        return [f"{indent}@gate(", *[f"{indent}    {a}," for a in args], f"{indent})"]
    one = f"{indent}@gate({', '.join(args)}, viewer_write={repr_str(viewer_write)})"
    if len(one) <= MAX:
        return [one]
    width = MAX - len(indent) - 8 - 3
    parts = textwrap.wrap(viewer_write, width=width, drop_whitespace=False, break_on_hyphens=False)
    out = [f"{indent}@gate(", *[f"{indent}    {a}," for a in args], f"{indent}    viewer_write=("]
    out += [f"{indent}        {repr_str(p)}" for p in parts]
    out += [f"{indent}    ),", f"{indent})"]
    return out


def repr_str(s: str) -> str:
    if '"' not in s:
        return '"' + s.replace("\\", "\\\\") + '"'
    return repr(s)


def _comment_lines(comments: list[str], indent: str) -> list[str]:
    out = []
    for c in comments:
        for ln in textwrap.wrap(c, width=MAX - len(indent) - 2, break_on_hyphens=False) or [""]:
            out.append(f"{indent}# {ln}")
    return out


def main() -> None:
    src = open(GATES_PY).read()
    cg, kg = _table_comments(src, "GATES", False)
    cv, kv = _table_comments(src, "VIEWER_MAY_MUTATE", False)
    cs, ks = _table_comments(src, "SETTINGS", True)
    cn, kn = _table_comments(src, "NOT_SETTINGS", False)
    assert set(kg) == set(rg.GATES) and len(kg) == len(rg.GATES), set(kg) ^ set(rg.GATES)
    assert set(kv) == set(rg.VIEWER_MAY_MUTATE), set(kv) ^ set(rg.VIEWER_MAY_MUTATE)
    assert set(ks) == set(rg.SETTINGS), set(ks) ^ set(rg.SETTINGS)
    assert set(kn) == set(rg.NOT_SETTINGS), set(kn) ^ set(rg.NOT_SETTINGS)

    views = _views()
    missing = sorted(set(rg.GATES) - set(views))
    assert not missing, f"table names ops with no view: {missing}"
    undeclared = sorted(op for op in views if op not in rg.GATES and rg.gate_of(views[op]) is None)
    assert not undeclared, f"views with no table entry: {undeclared}"

    inserts: dict[str, list[tuple[int, list[str]]]] = defaultdict(list)
    for op, labels in rg.GATES.items():
        fn = views[op]
        if rg.gate_of(fn) is not None:
            continue
        path = inspect.getsourcefile(fn)
        lines, start = inspect.getsourcelines(fn)
        i = next(j for j, ln in enumerate(lines)
                 if re.match(rf"\s*(async\s+)?def\s+{fn.__name__}\(", ln))
        def_line = start + i  # 1-based
        indent = re.match(r"\s*", lines[i]).group(0)
        comments = []
        for c in [*cg.get(op, []), *cs.get(op, []), *cn.get(op, []), *cv.get(op, [])]:
            if c not in comments:
                comments.append(c)
        deco = _render(labels, op in rg.SETTINGS, rg.NOT_SETTINGS.get(op), rg.VIEWER_MAY_MUTATE.get(op), indent)
        # One short comment rides on a one-line decorator; anything else sits above it.
        if len(comments) == 1 and len(deco) == 1 and len(deco[0]) + 3 + len(comments[0]) <= MAX:
            block = [f"{deco[0]}  # {comments[0]}"]
        else:
            block = _comment_lines(comments, indent) + deco
        inserts[path].append((def_line, block))

    touched = []
    for path, items in inserts.items():
        text = open(path).read()
        file_lines = text.split("\n")
        for def_line, block in sorted(items, reverse=True):
            file_lines[def_line - 1:def_line - 1] = block
        text = "\n".join(file_lines)
        if not re.search(r"^from apps\.api\.route_gates import .*\bgate\b", text, re.M):
            tree = ast.parse(text)
            first = next(n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))
                         and not (isinstance(n, ast.ImportFrom) and n.module == "__future__"))
            fl = text.split("\n")
            fl.insert(first.lineno - 1, IMPORT)
            text = "\n".join(fl)
        open(path, "w").write(text)
        touched.append(path)
    print(f"{sum(len(v) for v in inserts.values())} decorators in {len(touched)} files")
    if touched:
        subprocess.run(["uv", "run", "ruff", "check", "--select", "I", "--fix", "-q", *touched], cwd=ROOT)


if __name__ == "__main__":
    sys.exit(main())
