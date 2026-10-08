"""Package data for a capability's host: the in-VM idle watchdog and the CloudFormation template.

``asset_path("ondemand-capability.cfn.yaml")`` is the stack a capability deploys;
``idle-shutdown.sh`` + the two systemd units are what its AMI installs. See the
README's "On-demand capabilities" section for the contract they implement.
"""
from __future__ import annotations

from pathlib import Path

_DIR = Path(__file__).with_name("assets")


def asset_path(name: str) -> Path:
    """Absolute path of a shipped asset; ``name`` is a bare file name."""
    if not name or Path(name).name != name or name in (".", ".."):
        raise ValueError(f"asset name must be a bare file name, got {name!r}")
    path = _DIR / name
    if not path.is_file():
        raise FileNotFoundError(f"no on-demand asset named {name!r} in {_DIR}")
    return path
