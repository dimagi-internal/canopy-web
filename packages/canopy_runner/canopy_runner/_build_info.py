"""Build provenance, stamped at INSTALL time — the fallback copy.

`scripts/install-runner.sh` overwrites this file in its temp build tree (never in
the working checkout) before building the wheel, so an INSTALLED runner carries
the sha of the runner source it was built from. Running from a source checkout
leaves this file as-is and `main._code_sha()` computes the same quantity live
from git instead.

SHA is deliberately NOT the repo HEAD: it is the last commit that touched
`packages/canopy_runner/canopy_runner/`. HEAD moves on every canopy-web commit,
so comparing it against anything would alert on an unrelated frontend change.
See docs/superpowers/specs/2026-07-28-runner-as-installed-package-design.md.
"""
from __future__ import annotations

# Empty means "not stamped" — a source checkout, or a build that had no git
# available. Every consumer treats empty as "unknown" and stays quiet rather
# than guessing; a staleness alert on incomplete information is worse than none.
SHA = ""
BUILT_AT = ""
# Committer epoch of the commit named by SHA. 0 = unstamped/unknown, which orders
# nothing — consumers fall back to a direction-less "differs" rather than guessing.
COMMITTED_AT = 0
