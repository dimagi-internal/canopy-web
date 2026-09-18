"""Fail when an upper bound in pyproject.toml has quietly become a freeze.

**The pattern this exists for.** A cap like `<1.0` on a 0.x package reads as
caution and behaves as a freeze the day upstream ships 1.0 — and nothing says
so. `uv lock` resolves happily inside the cap, CI stays green, and the project
sits on an old release until somebody happens to look. This repo paid for that
four times before writing this:

    django-allauth   <1.0    allauth jumped 0.63 -> 64.x; the cap held us on 0.x
    anthropic        <1.0    the SDK reached 1.x; the cap froze us on 0.125
    fastmcp          <4.0    held back the subscription machinery
    ag-ui-protocol   <0.2    AG-UI 1.0 shipped; found by this script's first run

plus `pytest<9` and `pytest-asyncio<1.0`, found the same way.

**What it checks.** For every requirement with an upper bound, it asks PyPI for
the latest stable release and fails if the cap excludes it. A cap you mean to
keep is marked on the line(s) directly above the requirement:

    # cap-hold: 2.0 drops the sync client we still use (see #1234)
    "somepkg>=1.4,<2.0",

A hold must carry a reason, and a hold that no longer holds anything back — the
cap admits the latest release again — fails too. An allowlist nobody prunes
reads as considered long after it stopped being true (canopy learned that one
from `KNOWN_UNMAPPED` in `tests/test_agui_socket.py`, where every entry had a
reason and every reason was wrong).

**Why a scheduled job and not a required PR check.** The trigger is upstream
publishing something, which has nothing to do with whichever PR happens to be
open. A required check that fails on unrelated PRs teaches people to re-run it
until it goes away. It runs daily, and on PRs that touch pyproject.toml, where
the PR's author is exactly the right person to hear about it.

Usage: `uv run python scripts/check_dependency_caps.py [pyproject.toml]`
Exit 0 clean, 1 on findings, 2 if PyPI could not be asked about anything.
"""

from __future__ import annotations

import json
import re
import sys
import tomllib
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from packaging.requirements import Requirement
from packaging.version import InvalidVersion, Version

HOLD = re.compile(r"#\s*cap-hold:\s*(.*)$")


@dataclass(frozen=True)
class Finding:
    kind: str  # "frozen" | "stale-hold" | "hold-without-reason"
    name: str
    spec: str
    latest: str
    detail: str = ""

    def __str__(self) -> str:
        if self.kind == "frozen":
            return (f"{self.name}{self.spec} excludes the latest release, {self.latest}. "
                    f"Raise the cap, or mark it `# cap-hold: <why>` on the line above.")
        if self.kind == "stale-hold":
            return (f"{self.name}{self.spec} is marked cap-hold ({self.detail!r}) but already "
                    f"admits the latest release, {self.latest}. Delete the hold.")
        return f"{self.name}{self.spec} has `cap-hold:` with no reason. Say why it is held."


def _requirements(data: dict) -> list[str]:
    reqs = list(data.get("project", {}).get("dependencies", []))
    for group in data.get("project", {}).get("optional-dependencies", {}).values():
        reqs.extend(group)
    return reqs


def _hold_for(requirement: str, lines: list[str]) -> str | None:
    """The `cap-hold:` reason in the comment block directly above `requirement`.

    tomllib drops comments, so this reads the raw lines. Only the contiguous
    comment block immediately above counts — a hold three requirements up must
    not leak onto this one.
    """
    needle = f'"{requirement}"'
    for i, line in enumerate(lines):
        if needle not in line:
            continue
        j = i - 1
        while j >= 0 and lines[j].strip().startswith("#"):
            m = HOLD.search(lines[j])
            if m:
                return m.group(1).strip()
            j -= 1
        return None
    return None


def find_frozen(
    pyproject: str, latest_of: Callable[[str], Version | None]
) -> tuple[list[Finding], int, int]:
    """Every capped requirement whose cap excludes upstream's latest release.

    Pure apart from `latest_of`, so the rule is testable without a network.
    Returns (findings, how many capped packages were checked, how many could not be).
    """
    data = tomllib.loads(pyproject)
    lines = pyproject.splitlines()
    findings: list[Finding] = []
    checked = unchecked = 0
    seen: set[str] = set()

    for raw in _requirements(data):
        req = Requirement(raw)
        if not any(s.operator in ("<", "<=") for s in req.specifier):
            continue
        # `pydantic` and `pydantic[email]` share one cap; report it once.
        if req.name.lower() in seen:
            continue
        seen.add(req.name.lower())

        hold = _hold_for(raw, lines)
        if hold is not None and not hold:
            findings.append(Finding("hold-without-reason", req.name, str(req.specifier), ""))
            continue

        latest = latest_of(req.name)
        if latest is None:
            unchecked += 1
            continue
        checked += 1
        # A pre-release past the cap is not a freeze: nobody should be on it,
        # and `contains(prereleases=False)` answers False for EVERY pre-release,
        # so without this an `rc` would read as a stable release being blocked.
        if latest.is_prerelease or latest.is_devrelease:
            continue

        admits_latest = req.specifier.contains(latest, prereleases=False)
        if not admits_latest and hold is None:
            findings.append(Finding("frozen", req.name, str(req.specifier), str(latest)))
        elif admits_latest and hold:
            findings.append(Finding("stale-hold", req.name, str(req.specifier), str(latest), hold))

    return findings, checked, unchecked


def pypi_latest(name: str) -> Version | None:
    """Latest stable release on PyPI, or None if PyPI could not be asked."""
    try:
        with urllib.request.urlopen(f"https://pypi.org/pypi/{name}/json", timeout=15) as resp:
            return Version(json.load(resp)["info"]["version"])
    except (OSError, ValueError, KeyError, InvalidVersion):
        return None


def main(argv: list[str]) -> int:
    path = Path(argv[1]) if len(argv) > 1 else Path(__file__).resolve().parents[1] / "pyproject.toml"
    findings, checked, unchecked = find_frozen(path.read_text(), pypi_latest)

    for f in findings:
        print(f"::error file={path.name}::{f}")
    if unchecked:
        print(f"::warning::{unchecked} capped requirement(s) could not be checked against PyPI.")

    if findings:
        return 1
    # Nothing found is only a clean bill if we actually asked. A run where PyPI
    # answered nothing must not read as "every cap is fine".
    if checked == 0 and unchecked:
        print("::error::PyPI answered for none of the capped requirements; nothing was checked.")
        return 2
    print(f"{checked} capped package(s) admit their latest release ({unchecked} unchecked).")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
