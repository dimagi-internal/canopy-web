"""The one walker the source guards share: which files are production source, read and
parsed once per process.

A guard over source ("nothing outside X calls Y") is only as strong as the set of
files it reads. When every guard walked the tree itself, each chose its own excludes
— `"/migrations/" in rel` here, `"migrations" in path.parts` there, `tests.py`
skipped by one and scanned by the next — so two guards about the same code read
different code. Here there is one definition of a test file and one of a migration,
and a guard says only which directories it covers and what it leaves out by name.

    for src in iter_app_sources(("apps",)):
        for node in ast.walk(src.tree): ...

Matching is on the path RELATIVE to the repo, so a checkout that happens to sit
under a directory named `tests` or `migrations` still scans everything.
"""
from __future__ import annotations

import ast
import functools
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def is_test_file(rel: str) -> bool:
    """A test module or test support: anything under a `tests/` directory, any
    `test_*.py`, `*_tests.py` or `tests.py` (pytest's own patterns), and `conftest.py`."""
    parts = rel.split("/")
    name = parts[-1]
    return ("tests" in parts[:-1] or name.startswith("test_") or name.endswith("_tests.py")
            or name in ("tests.py", "conftest.py"))


def is_migration(rel: str) -> bool:
    return "migrations" in rel.split("/")[:-1]


@functools.cache
def _text(path: Path) -> str:
    return path.read_text()


@functools.cache
def _tree(path: Path) -> ast.Module:
    return ast.parse(_text(path), filename=str(path))


@dataclass(frozen=True)
class Source:
    path: Path
    rel: str  # posix, relative to the repo root

    @property
    def text(self) -> str:
        return _text(self.path)

    @property
    def tree(self) -> ast.Module:
        return _tree(self.path)

    def __iter__(self):  # `for path, rel, text, tree in iter_app_sources(...)`
        return iter((self.path, self.rel, self.text, self.tree))


def iter_app_sources(dirs: Iterable[str] = ("apps", "config"), *, include_tests: bool = False,
                     include_migrations: bool = False, exclude: Iterable[str] = ()) -> Iterator[Source]:
    """Every `.py` under `dirs` (repo-relative), sorted, once each.

    `exclude` holds repo-relative paths; one ending in `/` excludes that directory.
    """
    exclude = tuple(exclude)
    seen: set[Path] = set()
    for d in dirs:
        for path in sorted((ROOT / d).rglob("*.py")):
            if path in seen:
                continue
            seen.add(path)
            rel = path.relative_to(ROOT).as_posix()
            if not include_tests and is_test_file(rel):
                continue
            if not include_migrations and is_migration(rel):
                continue
            if any(rel == e or (e.endswith("/") and rel.startswith(e)) for e in exclude):
                continue
            yield Source(path, rel)
