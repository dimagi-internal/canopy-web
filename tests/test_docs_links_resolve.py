"""Every link into the architecture docs lands on a file and a heading that exist.

The topic docs are split by subject, and a split moves headings between files. A link
that still names the old file, or an `#anchor` whose heading moved, silently sends a
reader (or an agent) to the wrong page. This walks CLAUDE.md, ARCHITECTURE.md and
docs/architecture/*.md and checks:

* every relative markdown link `[text](path.md#anchor)` or `(#anchor)`: the file exists
  and the anchor is a heading slug in it;
* every `docs/architecture/<file>.md` reference (with an optional `#anchor` or
  `§ Heading`), in those docs and in source comments and docstrings: the file exists
  and the heading is in it.

Pure stdlib; no Django setup.
"""
from __future__ import annotations

import pathlib
import re

import pytest

pytestmark = pytest.mark.guard

ROOT = pathlib.Path(__file__).resolve().parent.parent
ARCH = ROOT / "docs" / "architecture"
DOCS = [ROOT / "CLAUDE.md", ROOT / "ARCHITECTURE.md", *sorted(ARCH.glob("*.md"))]
SOURCE_ROOTS = ["apps", "config", "tests", "scripts", "runner", "sdk", "packages", "frontend/src", "site", "templates"]
SOURCE_SUFFIXES = {".py", ".ts", ".tsx", ".js", ".mjs", ".astro", ".html", ".yml", ".yaml", ".toml"}
SKIP_DIRS = {"node_modules", ".venv", "dist", "build", "__pycache__", ".git", ".astro"}

HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
FENCE = re.compile(r"^\s*(```|~~~)")
MD_LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
ARCH_REF = re.compile(r"(?<!canopy )docs/architecture/([A-Za-z0-9_.-]+\.md)(?:#([A-Za-z0-9_-]+))?(?:`?\s*§\s*([^`.;,\n()]+))?")


def slug(heading: str) -> str:
    """GitHub's anchor for a heading: lowercase, drop punctuation, spaces to hyphens."""
    text = re.sub(r"`", "", heading)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"<[^>]+>", "", text).strip().lower()
    text = re.sub(r"[^\w\- ]", "", text, flags=re.UNICODE)
    return text.replace(" ", "-")


_headings: dict[pathlib.Path, tuple[set[str], list[str]]] = {}


def headings(path: pathlib.Path) -> tuple[set[str], list[str]]:
    """(anchor slugs, plain heading texts) of one markdown file, fenced code skipped."""
    if path not in _headings:
        slugs: set[str] = set()
        texts: list[str] = []
        seen: dict[str, int] = {}
        fenced = False
        for line in path.read_text(encoding="utf-8").splitlines():
            if FENCE.match(line):
                fenced = not fenced
                continue
            m = None if fenced else HEADING.match(line)
            if not m:
                continue
            base = slug(m.group(2))
            n = seen.get(base, 0)
            seen[base] = n + 1
            slugs.add(base if n == 0 else f"{base}-{n}")
            texts.append(re.sub(r"[`*_]", "", m.group(2)).strip().lower())
        _headings[path] = (slugs, texts)
    return _headings[path]


def _prose_lines(path: pathlib.Path):
    fenced = False
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if FENCE.match(line):
            fenced = not fenced
            continue
        if not fenced:
            yield n, line


def _section_exists(target: pathlib.Path, wanted: str) -> bool:
    wanted = re.sub(r"[`*_\"“”]", "", wanted).replace("’", "'").strip().lower().rstrip(" .:'")
    if not wanted:
        return True
    texts = [t.replace("’", "'") for t in headings(target)[1]]
    # A reference may quote a heading's start, or carry trailing prose after the whole heading.
    return any(t.startswith(wanted) or wanted in t or wanted.startswith(t) for t in texts)


def _check_arch_ref(where: str, name: str, anchor: str | None, section: str | None, problems: list[str]) -> None:
    target = ARCH / name
    if not target.is_file():
        problems.append(f"{where}: docs/architecture/{name} does not exist")
        return
    if anchor and anchor not in headings(target)[0]:
        problems.append(f"{where}: docs/architecture/{name}#{anchor} has no such heading")
    if section and not _section_exists(target, section):
        problems.append(f"{where}: docs/architecture/{name} has no heading matching '{section.strip()}'")


def test_markdown_links_resolve():
    problems: list[str] = []
    for doc in DOCS:
        rel = doc.relative_to(ROOT)
        for n, line in _prose_lines(doc):
            for m in MD_LINK.finditer(line):
                href = m.group(1)
                if re.match(r"[a-z][a-z0-9+.-]*:", href, re.I):
                    continue  # http(s):, mailto: — external
                path_part, _, anchor = href.partition("#")
                target = doc if not path_part else (doc.parent / path_part).resolve()
                where = f"{rel}:{n}"
                if not target.exists():
                    problems.append(f"{where}: link to {path_part} — no such file")
                elif anchor and target.suffix == ".md" and anchor not in headings(target)[0]:
                    problems.append(f"{where}: link to {path_part or doc.name}#{anchor} — no such heading")
    assert not problems, "Broken doc links:\n" + "\n".join(problems)


def test_architecture_doc_references_resolve():
    problems: list[str] = []
    for doc in DOCS:
        for n, line in _prose_lines(doc):
            for m in ARCH_REF.finditer(line):
                _check_arch_ref(f"{doc.relative_to(ROOT)}:{n}", m.group(1), m.group(2), m.group(3), problems)
    assert not problems, "Broken docs/architecture references:\n" + "\n".join(problems)


def test_source_references_to_architecture_docs_resolve():
    problems: list[str] = []
    for top in SOURCE_ROOTS:
        base = ROOT / top
        if not base.exists():
            continue
        for path in base.rglob("*"):
            if not path.is_file() or path.suffix not in SOURCE_SUFFIXES:
                continue
            if SKIP_DIRS & set(path.relative_to(ROOT).parts):
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            if "docs/architecture/" not in text:
                continue
            for n, line in enumerate(text.splitlines(), 1):
                for m in ARCH_REF.finditer(line):
                    _check_arch_ref(f"{path.relative_to(ROOT)}:{n}", m.group(1), m.group(2), m.group(3), problems)
    assert not problems, "Broken docs/architecture references in source:\n" + "\n".join(problems)
