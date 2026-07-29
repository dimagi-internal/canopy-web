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

__all__ = ["version", "code_branch", "code_sha", "runner_src_dir"]

_BRANCH_TTL_SECONDS = 15
_last_branch_check = 0.0
_cached_branch = ""


def version() -> str:
    return __version__


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
        src = runner_src_dir()
        out = subprocess.run(
            ["git", "-C", str(src), "log", "-1", "--format=%H", "--", str(src)],
            capture_output=True, text=True, timeout=5,
        )
        return out.stdout.strip() if out.returncode == 0 else ""
    except Exception:  # noqa: BLE001 — best-effort; never break the heartbeat
        return ""


def _reset_for_tests() -> None:
    global _last_branch_check, _cached_branch
    _last_branch_check = 0.0
    _cached_branch = ""
    # getattr: a test may have monkeypatched code_sha with a plain function, and
    # teardown ordering means this can run while that patch is still in place.
    getattr(code_sha, "cache_clear", lambda: None)()
