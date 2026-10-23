"""A runner's egress lock, as a person sets it in canopy-web (spec
2026-10-22-phi-agents-on-zdr-runners §1, "Egress locked").

The box enforces it (`runner/ec2/cloud_runner.py`: a loopback proxy per unit plus
root's `CANOPY-EGRESS` chain). canopy-web holds what a person decided, per runner:
locked or not, and the hosts it admits beyond the box's base set. Root on the box
reads it by its instance role (`POST /api/harness/runners/box-egress`) and applies
it on top of its own floor — the box never loosens below what its `box.env` says,
and a ZDR box (`runner_requirements.is_zdr_box`) is locked whatever is set here.

Loosening a ZDR box (adding a host) is a person's act in a browser behind a passkey;
a token may tighten it. Any other box: its administrators, from any client.
"""
from __future__ import annotations

import re

from .runner_requirements import is_zdr_box

#: A hostname, or `.example.com` for every subdomain of example.com — the box's
#: own `RUNNER_EGRESS_ALLOW` grammar (cloud_runner.egress_allowlist).
_HOSTNAME = re.compile(
    r"^\.?(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$")
MAX_HOSTS = 100
FORCED = "a ZDR box's egress is always locked"


def normalize_allow(items) -> list[str]:
    """Lower-cased, de-duplicated and sorted; ValueError naming the first entry that
    is not a hostname. A bare label (`localhost`) is refused: it names nothing public."""
    out = set()
    for raw in items or []:
        host = str(raw).strip().lower().rstrip(".")
        if not host:
            continue
        if not _HOSTNAME.match(host):
            raise ValueError(f"{raw!r} is not a hostname (or a .suffix like .example.com)")
        out.add(host)
    if len(out) > MAX_HOSTS:
        raise ValueError(f"at most {MAX_HOSTS} hosts")
    return sorted(out)


def effective(runner) -> dict:
    """What the box is told: `locked` (a ZDR box always is) and `allow`."""
    forced = is_zdr_box(runner)
    return {"locked": bool(runner.egress_locked or forced), "allow": list(runner.egress_allow or []),
            "forced": forced}


def loosens(runner, *, locked: bool, allow: list[str]) -> bool:
    """Whether this change lets the box reach more than it does now."""
    return (runner.egress_locked and not locked) or bool(set(allow) - set(runner.egress_allow or []))
