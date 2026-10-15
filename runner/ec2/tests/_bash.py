"""Shared helpers for tests that run single functions out of bootstrap_agents.sh:
a bash with associative arrays (macOS ships 3.2), and `_fn` to extract one function.
Moved here from test_readiness_is_observed.py when the per-agent bash it tested moved to
canopy (`canopy agent prepare`, 2026-10-15)."""
from __future__ import annotations

import pathlib
import shlex
import shutil
import subprocess

import pytest


# `verify_mailbox` reads associative arrays, so unlike test_client_creds_says_why
# these genuinely need bash 4+. Rather than skip on macOS — which is how this
# directory shipped a test CI was the first thing to ever execute — look for a
# real bash 5 first (`brew install bash`) and only skip if the machine has none.
def _has_assoc_arrays(candidate: str | None) -> bool:
    """Probe a candidate bash. Must swallow FileNotFoundError, not just a bad
    exit code: on Linux CI /opt/homebrew/bin/bash simply does not exist, and an
    uncaught OSError here fails COLLECTION — every test in the file errors
    rather than skipping, which is a worse outcome than the skip it replaced."""
    if not candidate:
        return False
    try:
        return subprocess.run(
            [candidate, "-c", "declare -A _t=( [k]=v )"], capture_output=True
        ).returncode == 0
    except OSError:
        return False


# `bash` from PATH first — that is what CI has (Linux ships 5.x), and on a Mac
# with `brew install bash` it is the brew one. The explicit paths are the
# fallback for a Mac whose PATH still leads to /bin/bash 3.2.
BASH = next(
    (b for b in (shutil.which("bash"), "/opt/homebrew/bin/bash", "/usr/local/bin/bash")
     if _has_assoc_arrays(b)),
    None,
)
pytestmark = pytest.mark.skipif(
    BASH is None,
    reason="no bash with associative arrays (macOS ships 3.2; `brew install bash`)",
)

SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "bootstrap_agents.sh"


def _fn(name: str) -> str:
    lines = SCRIPT.read_text().splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(f"{name}() {{"))
    end = next(i for i in range(start + 1, len(lines)) if lines[i] == "}")
    return "\n".join(lines[start:end + 1])
