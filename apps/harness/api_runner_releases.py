"""`/api/harness/runner-releases/…` — the deployed runner version's installers and update.

`current` is what a bundled runner's updater asks with its own token
(`runner_auth` lets a runner token reach it); `downloads` is what the download
buttons list. Both name only the deployed `RUNNER_CODE_SHA` (`runner_releases`).
"""
from __future__ import annotations

from django.http import HttpRequest
from ninja import Router
from ninja.errors import HttpError

from apps.api.auth import session_auth

from . import runner_releases as rel
from .schemas import RunnerDownloadOut, RunnerDownloadsOut, RunnerReleaseOut

router = Router(auth=session_auth, tags=["harness"])


def _manifest_or_503(sha: str) -> dict | None:
    try:
        return rel.manifest(sha)
    except rel.NotConfigured as exc:
        raise HttpError(503, "runner downloads are not configured on this deployment") from exc


@router.get("/current", response=RunnerReleaseOut, summary="The deployed runner version's update for a platform")
def current_runner_release(request: HttpRequest, platform: str):
    """The update artifact of the runner version this deployment ships, for
    `platform` (`macos-arm64`, `macos-x86_64`, `windows-x86_64`): its sha, file,
    sha256, size, whether it is signed (and notarized), and a download `url`
    good for 15 minutes. 404 when no release of that version is built for the
    platform — an updater then does nothing. 503 when downloads are off."""
    if platform not in rel.PLATFORMS:
        raise HttpError(422, f"unknown platform '{platform}'; one of {', '.join(rel.PLATFORMS)}")
    sha = rel.deployed_sha()
    if not sha:
        raise HttpError(404, "no deployed runner version")
    art = rel.artifact(_manifest_or_503(sha), platform, "update")
    if art is None:
        raise HttpError(404, f"no runner release built for {platform} at {sha[:12]}")
    return RunnerReleaseOut(sha=sha, platform=platform, url=rel.presign(sha, art["file"]), **art)


@router.get("/downloads", response=RunnerDownloadsOut, summary="Download links for Canopy Runner")
def runner_downloads(request: HttpRequest):
    """The installers of the runner version this deployment ships, one per
    platform built — empty when none is. `href` signs you in if needed and
    redirects to the file. `signed: false` is an unsigned build: the operating
    system will warn before opening it."""
    sha = rel.deployed_sha()
    if not sha:
        return RunnerDownloadsOut(sha="", downloads=[])
    man = _manifest_or_503(sha)
    out = []
    for platform, label in rel.PLATFORMS.items():
        art = rel.artifact(man, platform, "installer")
        if art is not None:
            out.append(RunnerDownloadOut(platform=platform, label=label, href=rel.download_href(platform),
                                         **{k: art[k] for k in ("file", "size", "signed", "notarized")}))
    return RunnerDownloadsOut(sha=sha, downloads=out)
