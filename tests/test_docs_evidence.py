"""A doc cannot cite what is not in the code.

CLAUDE.md and every `docs/architecture/*.md` name paths, `path::symbol` pointers,
`module.function` pairs, tests, routes, settings and identifiers in backticks. A rename
that leaves one behind turns the doc into a confident wrong answer for the agent reading
it. This resolves every such token against the tracked files, so the rename fails here,
naming the doc line.

How a token is judged (see `check`, by its shape; an unrecognised shape passes):

* `a/b.py`, `a/b.py::symbol`, `a/dir/`, `a/*/c.py`: a tracked file or directory (glob and
  `<x>`/`{x}` placeholders allowed); the symbol must still occur in that file;
* `name.ext` alone: some tracked file has that basename;
* `module.name`: `apps/**/module.py` (or package) still mentions `name`; `Model.name` the
  file that defines the class;
* `test_x`: a `def` in a test file;
* `GET /api/...` / `/api/...`: a route decorator or URL pattern ends the same way;
* `manage.py cmd`: a management command; `SOME_SETTING`: appears in the source;
* a code-looking identifier (`snake_case`, `CamelCase`) of five characters or more: occurs
  in the source (docs themselves are not source).

`.docs-ignore` (repo root) lists what is legitimately not resolvable here: one
`token  # reason` per line; `*` matches any run of characters. An entry that no doc
token needs any more fails (stale): remove it.
"""

from __future__ import annotations

import pathlib
import re
import subprocess

import pytest

pytestmark = pytest.mark.guard

ROOT = pathlib.Path(__file__).resolve().parent.parent
ARCH = ROOT / "docs" / "architecture"
DOCS = [ROOT / "CLAUDE.md", *sorted(ARCH.glob("*.md"))]
IGNORE_FILE = ROOT / ".docs-ignore"
TOP_DIRS = {
    "apps",
    "frontend",
    "runner",
    "config",
    "deploy",
    "docs",
    "tests",
    "scripts",
    "packages",
    "sdk",
    "site",
    "templates",
    "local_apps",
    "assets",
    ".github",
}
FILE_EXT = r"(?:py|tsx?|md|ya?ml|html|sh|toml|cfg|css|astro|mjs|ps1|iss|plist|template)"
CORPUS_SUFFIXES = {
    ".py",
    ".ts",
    ".tsx",
    ".js",
    ".mjs",
    ".astro",
    ".html",
    ".yml",
    ".yaml",
    ".toml",
    ".sh",
    ".json",
    ".cfg",
    ".css",
    ".sql",
    ".txt",
    ".template",
    ".iss",
    ".ps1",
    ".plist",
}

_TOKEN = re.compile(r"`([^`\n]+)`")
_FENCE = re.compile(r"^\s*(```|~~~)")

FIX = (
    "Fix: open the code and change the doc to what it says today (the current name, path or "
    "symbol), or delete the claim if the code no longer does it. Add to .docs-ignore only for "
    "something that is not in this repository, with the reason."
)


def _git_files() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "-co", "--exclude-standard"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout
    return [p for p in out.splitlines() if (ROOT / p).is_file()]


@pytest.fixture(scope="module")
def corpus():
    files = _git_files()
    fileset = set(files)
    dirs = {
        str(pathlib.PurePosixPath(f).parents[i])
        for f in files
        for i in range(len(pathlib.PurePosixPath(f).parents) - 1)
    }
    basenames = {pathlib.PurePosixPath(f).name for f in files}
    texts: dict[str, str] = {}
    for f in files:
        p = pathlib.PurePosixPath(f)
        if (
            p.suffix in CORPUS_SUFFIXES
            and p.parts[0] != "docs"
            and f != "tests/test_docs_evidence.py"
            and "node_modules" not in p.parts
            and f != "uv.lock"
        ):
            try:
                texts[f] = (ROOT / f).read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
    all_text = "\n".join(texts.values())
    words = set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", all_text))
    strings = set(re.findall(r"[\"'`]([^\"'`\n]{1,100})[\"'`]", all_text))
    modules: dict[str, list[str]] = {}
    classes: dict[str, list[str]] = {}
    tests: set[str] = set()
    routes: list[tuple[str, str]] = []
    for f, t in texts.items():
        p = pathlib.PurePosixPath(f)
        if p.suffix in {".ts", ".tsx"} and "tests" not in p.parts:
            modules.setdefault(p.stem, []).append(f)
        if p.suffix == ".py":
            modules.setdefault(p.stem, []).append(f)
            if p.stem == "__init__":
                modules.setdefault(p.parent.name, []).append(f)
            for m in re.finditer(r"^\s*class (\w+)", t, re.M):
                classes.setdefault(m.group(1), []).append(f)
            if "tests" in p.parts or p.name.startswith("test_"):
                tests |= set(re.findall(r"^\s*(?:async )?def (test_\w+)", t, re.M))
            routes += [
                (m.group(1), m.group(2))
                for m in re.finditer(r"@\w*(?:router|api)\.(get|post|put|patch|delete)\(\s*[\"']([^\"']*)[\"']", t)
            ]
            routes += [("any", m.group(1)) for m in re.finditer(r"(?:re_)?path\(\s*r?[\"']([^\"']+)[\"']", t)]
    prefixes = {""}
    for t in texts.values():
        prefixes |= {_norm_route(m.group(1)) for m in re.finditer(r"add_router\(\s*[\"']([^\"']*)[\"']", t)}
        prefixes |= {_norm_route(m.group(1)) for m in re.finditer(r"Router\([^)]*prefix=[\"']([^\"']*)[\"']", t)}
    full_routes = {(m, _strip_api((pre + "/" + _norm_route(h)).strip("/"))) for m, h in routes for pre in prefixes}
    packages: dict[str, list[str]] = {}
    for f in texts:
        p = pathlib.PurePosixPath(f)
        if p.suffix == ".py" and "tests" not in p.parts:
            for parent in p.parents:
                if parent.name:
                    packages.setdefault(parent.name, []).append(f)
    commands = {pathlib.PurePosixPath(f).stem for f in files if "/management/commands/" in f}
    by_method: dict[str, set[str]] = {}
    for meth, have in full_routes:
        by_method.setdefault(meth, set()).add(have)
    dotted_strings = {s for s in strings if "." in s}
    return {
        "files": fileset,
        "dotted_strings": dotted_strings,
        "filelist": files,
        "dirs": dirs,
        "basenames": basenames,
        "texts": texts,
        "words": words,
        "modules": modules,
        "classes": classes,
        "tests": tests,
        "routes": routes,
        "commands": commands,
        "packages": packages,
        "all_text": all_text,
        "full_routes": full_routes,
        "by_method": by_method,
        "strings": strings,
        "full_routes_all": {h for _, h in full_routes},
    }


def _strip_api(p: str) -> str:
    for pre in ("api/w/{}/", "api/"):
        if p.startswith(pre):
            return p[len(pre) :]
    return p


def _norm_route(p: str) -> str:
    p = re.sub(r"\[[^\]]*\]|\^|\$", "", p)
    p = re.sub(r"\{[^}]*\}|<[^>]*>|:\w+", "{}", p.split("?")[0])
    return p.strip("/")


def _path_pattern(tok: str) -> re.Pattern[str]:
    parts = re.split(r"(\*+|<[^>]*>|\{[^}]*\})", tok)
    rx = "".join(
        "[^/]*"
        if x.startswith("*") and len(x) == 1
        else ".*"
        if x.startswith("**")
        else "[^/]+"
        if x[:1] in "<{" and len(x) > 1
        else re.escape(x)
        for x in parts
    )
    return re.compile(rx + "$")


def _word_in(name: str, text: str) -> bool:
    return re.search(rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])", text) is not None


_cache: dict[tuple[int, str], str | None] = {}


def check(tok: str, c: dict) -> str | None:
    key = (id(c), tok)
    if key not in _cache:
        _cache[key] = _check(tok, c)
    return _cache[key]


def _check(tok: str, c: dict) -> str | None:
    """Why this token does not resolve, or None (resolves, or not a shape this net judges)."""
    tok = tok.strip().rstrip(".,;:")
    if (
        not tok
        or " " in tok.replace("::", "")
        and not re.match(r"^(GET|POST|PUT|PATCH|DELETE) /\S*$|^manage\.py \w+$", tok)
    ):
        return None
    if tok.startswith(("http", "~", "$", "s3:", "<", "-", "@", "#", "…", "...", "{")) or "…" in tok:
        return None
    if tok.startswith("/") and not tok.startswith("/api/"):
        return None  # a URL path or an absolute path on a box: not judged
    # METHOD /path
    m = re.fullmatch(r"(GET|POST|PUT|PATCH|DELETE)(?:\|\w+)* (/\S*)", tok)
    if m:
        return _route(m.group(1).lower(), m.group(2), c)
    m = re.fullmatch(r"manage\.py (\w+)", tok)
    if m:
        return None if m.group(1) in c["commands"] else "no such management command"
    tok = re.sub(r"\(.*\)$", "", tok)  # foo(args) / foo()
    # test name
    if re.fullmatch(r"test_\w+", tok):
        return None if tok in c["tests"] else "no such test function in the tests"
    # path or path::symbol
    path, _, sym = tok.partition("::")
    if "/" in path or re.fullmatch(rf"[\w.\-*]+\.{FILE_EXT}", path):
        return _path(path, sym, c, tok)
    if tok.startswith("/api/"):
        return _route("any", tok, c)
    # Model.attr / module.name
    m = re.fullmatch(r"([A-Za-z_]\w*)\.([A-Za-z_]\w*)(?:\.\w+)*", tok)
    if m and not re.fullmatch(rf".*\.{FILE_EXT}", tok):
        head, attr = m.groups()
        if tok in c["strings"] or any(tok in s for s in c["dotted_strings"]):
            return None  # an event name, key or permission the code spells out literally
        if head[0].isupper() and head in c["classes"]:
            return (
                None
                if any(_word_in(attr, c["texts"][f]) for f in c["classes"][head])
                else f"class {head} no longer mentions {attr}"
            )
        if attr in c["strings"]:
            return None  # a key or value the code spells out as a string
        if head[0].islower() and head in c["modules"]:
            return (
                None
                if any(_word_in(attr, c["texts"][f]) for f in c["modules"][head])
                or any(_word_in(attr, c["texts"][f]) for f in c["packages"].get(head, []))
                else f"module {head} no longer mentions {attr}"
            )
        return None
    # settings / constants / identifiers
    if re.fullmatch(r"[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+", tok):
        return None if tok in c["words"] else "appears nowhere in the source"
    if re.fullmatch(r"[A-Za-z_]\w{4,}", tok) and (
        ("_" in tok.strip("_"))
        or re.search(r"[a-z][A-Z]", tok)
        or tok[0].isupper()
        and re.search(r"[a-z]", tok)
        and re.search(r"[A-Z]", tok[1:])
    ):
        return None if tok in c["words"] or tok in c["tests"] else "appears nowhere in the source"
    return None


def _path(path: str, sym: str, c: dict, tok: str) -> str | None:
    p = path.lstrip("./") if path.startswith("./") else path
    top = p.split("/")[0]
    if "/" in p and top not in TOP_DIRS and not re.search(rf"\.{FILE_EXT}$", p):
        return None  # URL-ish or other-repo directory: not judged
    if any(ch in p for ch in "*<>{}"):
        rx = _path_pattern(p)
        hit = any(rx.search(f) for f in c["filelist"]) or any(rx.search(d + "/") or rx.search(d) for d in c["dirs"])
        return None if hit else "no tracked file matches"
    q = p.rstrip("/")
    dotted = re.fullmatch(r"(.+)\.(\w+)", q)
    if q not in c["files"] and dotted and not sym and not re.search(rf"\.{FILE_EXT}$", q):
        for ext in (".py", ".ts", ".tsx", "/__init__.py"):
            if dotted.group(1) + ext in c["files"]:
                return (
                    None
                    if _word_in(dotted.group(2), c["texts"].get(dotted.group(1) + ext, ""))
                    else f"{dotted.group(1)}{ext} no longer mentions {dotted.group(2)}"
                )
    if q in c["files"]:
        if sym and q in c["texts"] and not _word_in(sym.split(".")[-1].rstrip("()"), c["texts"][q]):
            return f"{q} no longer mentions {sym}"
        return None
    if q in c["dirs"] or (ROOT / q).is_dir():
        return None
    if not re.search(r"\.\w+$", q) and (q + ".py" in c["files"] or q + "/__init__.py" in c["files"]):
        return None
    if top not in TOP_DIRS:  # relative or bare file: a unique suffix or basename
        if "/" not in q:
            ok = q in c["basenames"]
        else:
            ok = any(f.endswith("/" + q) or f == q for f in c["filelist"]) or any(
                d.endswith("/" + q) for d in c["dirs"]
            )
        if not ok:
            return "no tracked file with that name"
        if sym:
            files = [f for f in c["filelist"] if f.endswith("/" + q) or f == q]
            if not any(f in c["texts"] and _word_in(sym.split(".")[-1], c["texts"][f]) for f in files):
                return f"{q} no longer mentions {sym}"
        return None
    return "no such file or directory"


def _route(method: str, path: str, c: dict) -> str | None:
    path = re.sub(r"\[[^\]]*\]", "", path).split("?")[0]
    if path in c["strings"]:
        return None  # a path the code spells out as a constant
    for alt in path.split("|"):
        want = _strip_api(_norm_route(alt if alt.startswith("/") else path.rsplit("/", 1)[0] + "/" + alt))
        if not want:
            return None
        pool = (
            c["by_method"].get("any", set()) | c["by_method"].get(method, set())
            if method != "any"
            else c["full_routes_all"]
        )
        if want not in pool:
            return "no route decorator or URL pattern ends this way"
    return None


def _load_ignore() -> list[tuple[str, str]]:
    entries = []
    if IGNORE_FILE.is_file():
        for n, line in enumerate(IGNORE_FILE.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            tok, sep, why = line.partition("  # ")
            assert sep and why.strip(), f".docs-ignore:{n}: every entry needs a reason: `token  # reason`"
            entries.append((tok.strip(), why.strip()))
    return entries


def _doc_tokens() -> list[tuple[str, int, str]]:
    out = []
    for doc in DOCS:
        fenced = False
        for n, line in enumerate(doc.read_text(encoding="utf-8").splitlines(), 1):
            if _FENCE.match(line):
                fenced = not fenced
                continue
            if fenced:
                continue
            for tok in _TOKEN.findall(line):
                out.append((str(doc.relative_to(ROOT)), n, tok.strip()))
    return out


def _matches(pattern: str, tok: str) -> bool:
    """Only `*` is a wildcard (brackets and braces in a token are literal)."""
    return re.fullmatch(".*".join(re.escape(part) for part in pattern.split("*")), tok) is not None


def _ignored(tok: str, entries: list[tuple[str, str]]) -> str | None:
    t = tok.strip().rstrip(".,;:")
    for pat, _ in entries:
        if _matches(pat, t) or _matches(pat, tok):
            return pat
    return None


def test_every_token_the_docs_cite_resolves(corpus):
    entries = _load_ignore()
    bad, seen = [], 0
    for doc, n, tok in _doc_tokens():
        seen += 1
        if _ignored(tok, entries):
            continue
        why = check(tok, corpus)
        if why:
            bad.append(f"  {doc}:{n}: `{tok}` — {why}")
    assert seen > 1000, "the docs lost their citations, or the token pattern broke"
    assert not bad, f"{len(bad)} thing(s) the docs cite are not in the code.\n" + FIX + "\n" + "\n".join(bad)


def test_ignore_file_has_no_stale_entries(corpus):
    """An entry that no doc token needs any more is a hole in the net: remove it."""
    entries = _load_ignore()
    tokens = _doc_tokens()
    stale = []
    for pat, _ in entries:
        needed = any(
            _matches(pat, tok.strip().rstrip(".,;:")) or _matches(pat, tok)
            for _, _, tok in tokens
            if check(tok, corpus)
        )
        if not needed:
            stale.append(f"  `{pat}`")
    assert not stale, (
        "These .docs-ignore entries match no unresolvable doc token (the doc changed, or the code now "
        "has it). Remove them:\n" + "\n".join(stale)
    )


def test_the_checker_catches_rot(corpus):
    """The net itself: made-up citations fail, real ones pass."""
    for tok in (
        "apps/agents/no_such_file.py",
        "apps/agents/roles.py::no_such_symbol_anywhere",
        "test_a_thing_nobody_wrote",
        "GET /api/nothing-here-at-all",
        "manage.py imagined_command",
        "CANOPY_NOT_A_SETTING_ANYWHERE",
        "roles.no_such_function_here",
        "some_imagined_helper_name",
    ):
        assert check(tok, corpus), tok
    for tok in (
        "apps/agents/roles.py",
        "tests/test_docs_evidence.py",
        "test_the_checker_catches_rot",
        "CANOPY_RETENTION_ENFORCE",
        "manage.py retention_sweep",
    ):
        assert check(tok, corpus) is None, tok
