"""Runner release and download schemas."""
from __future__ import annotations

from ninja import Schema


class RunnerReleaseOut(Schema):
    """The update artifact of the deployed runner version for one platform.
    `url` is a presigned download, good for 15 minutes."""

    sha: str
    platform: str
    file: str
    url: str
    sha256: str
    size: int
    signed: bool
    notarized: bool


class RunnerDownloadOut(Schema):
    platform: str
    label: str
    file: str
    size: int
    signed: bool
    notarized: bool
    #: Same-origin link: signs you in if needed, then redirects to the bytes.
    href: str


class RunnerDownloadsOut(Schema):
    """The installers of the deployed runner version; empty when none is built."""

    sha: str
    downloads: list[RunnerDownloadOut]

# --- direct use: private runners + direct turns (apps/harness/local_turns.py)
