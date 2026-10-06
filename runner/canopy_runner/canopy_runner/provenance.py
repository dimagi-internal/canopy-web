"""What code is this runner actually executing?

Lives in its own module (rather than in `main`) so `client` can stamp every
heartbeat with it without importing `main` — which imports `client`. That
direction matters: the alternative was passing these at each call site, and
there are six, four of which never passed `code_branch` at all. Since
`services.heartbeat` assigns unconditionally, each of those silently RESET the
field to "" — so the #306 wrong-branch banner would clear itself for a tick
whenever a lease-renewal or `--drain-one` heartbeat landed between two loop
ticks. One stamping point removes the whole class.

See docs/superpowers/specs/2026-07-28-runner-as-installed-package-design.md.
"""
from __future__ import annotations

import functools
import subprocess
import time
from pathlib import Path

from . import __version__

__all__ = ["version", "code_branch", "code_sha", "code_committed_at", "runner_src_dir"]

_BRANCH_TTL_SECONDS = 15
_last_branch_check = 0.0
_cached_branch = ""


def version() -> str:
    return __version__


#: What "the runner's code" means for staleness, repo-relative. Not only the Python
#: package: the installer, the menu-bar app and the icons it bundles ship to a box
#: through the same auto-update, and a change to any of them that did not mark the
#: box stale never reached it (the 2026-10-06 mark sat installed-nowhere-visible).
#: The SAME list is in .github/workflows/deploy-labs.yml (what the server expects)
#: and install-runner.sh (what an install stamps); tests/test_runner_code_paths.py
#: fails if the three drift, because a mismatch is a box marked stale forever.
CODE_PATHS = ('runner/canopy_runner/canopy_runner', 'runner/canopy_runner/scripts', 'runner/menubar', 'assets/brand')


def runner_src_dir() -> Path:
    """This package's own source directory — the path whose git history defines
    "the runner's code", separately from the rest of canopy-web."""
    return Path(__file__).resolve().parent


def code_branch(now_fn=time.monotonic) -> str:
    """The git branch of the runner's OWN checkout (best-effort, throttled+cached).

    Only ever non-empty for a SOURCE-mode runner: an installed runner lives in a
    tool venv with no repository, which is the point. Anything but `main` means
    another process left that checkout on a branch and the daemon is executing
    stale/wrong code (observed three times; a DDD run checking out a branch in the
    runner's shared checkout).

    Re-read on a TTL rather than pinned at startup, deliberately, and unlike
    `code_sha`: files the runner spawns rather than imports — the CDP sidecar —
    change the moment the branch does, so this must reflect the checkout NOW.
    Empty if it can't be determined; never raises (a heartbeat must not depend
    on git)."""
    global _last_branch_check, _cached_branch
    if now_fn() - _last_branch_check < _BRANCH_TTL_SECONDS:
        return _cached_branch
    _last_branch_check = now_fn()
    try:
        repo = runner_src_dir().parents[2]  # …/runner/canopy_runner/canopy_runner -> repo root
        out = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, timeout=3,
        )
        _cached_branch = out.stdout.strip() if out.returncode == 0 else ""
    except Exception:  # noqa: BLE001 — best-effort; never break the heartbeat
        _cached_branch = ""
    return _cached_branch


@functools.cache
def code_sha() -> str:
    """The sha of the last commit that touched the RUNNER'S OWN source.

    NOT the repo HEAD: HEAD moves on every canopy-web commit, so comparing it
    against the server's expectation would shout "update your runner" at a
    frontend CSS change. `git log -1 -- <runner src>` moves only when the runner
    moves, and the server computes the identical quantity at image-build time —
    comparing the same number on both sides is the whole point.

    Two provenances, one meaning:
      - INSTALLED: `_build_info.SHA`, stamped by install-runner.sh at build time.
      - SOURCE: computed live from the checkout this file sits in.
    Empty when neither is available (git missing, not a checkout, shallow clone);
    every consumer treats empty as "unknown" and stays silent.

    Cached for the process's lifetime ON PURPOSE — this answers "which code did I
    IMPORT", and that is fixed at process start. A `git pull` under a running
    daemon does not change the code in memory, so reporting the new sha before
    the restart would clear the staleness banner while still executing the old code.
    """
    from . import _build_info
    if _build_info.SHA:
        return _build_info.SHA
    try:
        repo = runner_src_dir().parents[2]
        out = subprocess.run(
            ["git", "-C", str(repo), "log", "-1", "--format=%H", "--", *CODE_PATHS],
            capture_output=True, text=True, timeout=5,
        )
        return out.stdout.strip() if out.returncode == 0 else ""
    except Exception:  # noqa: BLE001 — best-effort; never break the heartbeat
        return ""


@functools.cache
def code_committed_at() -> int:
    """Committer epoch of the commit `code_sha()` names — the ORDER a sha lacks.

    A sha is an identity, so `code_sha != expected` can only say DIFFERENT. The
    supervisor was rendering that as "behind", which is one of three possibilities
    (older, newer, divergent) asserted as though it were the only one — and on
    2026-07-29 it told the operator to update the most current box in the fleet,
    which had been installed from main ahead of the deploy that ships it. An alert
    that fires on the box you just fixed is one you learn to ignore.

    A timestamp rather than a version number, for the reason `code_version` is
    documented as decorative: it needs no human to remember it. `--format=%ct` on
    the same path-scoped `git log -1` that already yields `%H`, so the two always
    describe the SAME commit — a timestamp from a different commit would order two
    things nobody is comparing.

    0 means UNKNOWN and orders nothing; consumers fall back to a direction-less
    "differs" rather than guessing. Cached for the process's lifetime for exactly
    the reason `code_sha` is: it describes the code that was IMPORTED.
    """
    from . import _build_info
    stamped = getattr(_build_info, "COMMITTED_AT", 0)
    if stamped:
        return int(stamped)
    try:
        repo = runner_src_dir().parents[2]
        out = subprocess.run(
            ["git", "-C", str(repo), "log", "-1", "--format=%ct", "--", *CODE_PATHS],
            capture_output=True, text=True, timeout=5,
        )
        return int(out.stdout.strip()) if out.returncode == 0 else 0
    except Exception:  # noqa: BLE001 — incl. a non-numeric stdout; never break a heartbeat
        return 0


def _reset_for_tests() -> None:
    global _last_branch_check, _cached_branch
    _last_branch_check = 0.0
    _cached_branch = ""
    # getattr: a test may have monkeypatched code_sha with a plain function, and
    # teardown ordering means this can run while that patch is still in place.
    getattr(code_sha, "cache_clear", lambda: None)()
    getattr(code_committed_at, "cache_clear", lambda: None)()
