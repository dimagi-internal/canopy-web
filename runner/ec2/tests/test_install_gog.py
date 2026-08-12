"""`install_gog` must survive the gogcli tarball's layout, because it is not ours.

Same approach as test_update_runner_sh.py: run the real bash against real tarballs,
because the whole content of this function is which commands run against what.

THE INCIDENT. A from-scratch cloud rebuild on 2026-08-12 died here:

    tar: gog: Not found in archive
    FAIL: could not extract 'gog' from .../gogcli_0.35.0_linux_amd64.tar.gz
    WARN: gog install failed — per-agent gmail steps below will be skipped

gogcli 0.35.0 stores the binary as `./gog`, and `tar -xzf … gog` does not match
that member. gog never installed, so step 2 skipped the keyring and the
account->client map, and step 3 skipped the gmail-token import for ALL FIVE
agents. The box came up online, ready, claiming turns, and unable to send a
single email. It had gone unnoticed because the previous box was built on
2026-07-26, when the layout still happened to match — a latent break that only
fires on a rebuild, which is exactly the property that makes rebuilding worth
testing.

So: never name the member. Extract, then find the binary wherever it landed.
"""
from __future__ import annotations

import pathlib
import subprocess
import tarfile

import pytest

SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "bootstrap_agents.sh"


def _extract_function(path: pathlib.Path, name: str) -> str:
    """The literal text of one top-level bash function, `name() {` to its closing `}`."""
    lines = path.read_text().splitlines()
    start = next(i for i, l in enumerate(lines) if l.startswith(f"{name}() {{"))
    end = next(i for i in range(start + 1, len(lines)) if lines[i] == "}")
    return "\n".join(lines[start:end + 1])


def _make_tarball(dst: pathlib.Path, member: str) -> pathlib.Path:
    """A gogcli-shaped tarball storing an executable gog at `member`."""
    binary = dst / "gog-src"
    binary.write_text("#!/bin/sh\necho gog\n")
    binary.chmod(0o755)
    tgz = dst / "gogcli_9.9.9_linux_amd64.tar.gz"
    with tarfile.open(tgz, "w:gz") as tf:
        tf.add(binary, arcname=member)
    return tgz


def _run_install_gog(tmp_path: pathlib.Path, tarball: pathlib.Path) -> subprocess.CompletedProcess:
    """Source the script and call install_gog with the download already staged.

    `gh` and `curl` are stubbed to no-ops and the tarball is pre-placed in the temp
    dir install_gog creates, so the test exercises the EXTRACT path — the part that
    broke — without reaching the network.
    """
    home = tmp_path / "home"
    (home / ".local" / "bin").mkdir(parents=True)
    stub_bin = tmp_path / "stubbin"
    stub_bin.mkdir()
    for name in ("gh", "curl", "sudo"):
        # sudo must FAIL so install_gog takes its documented no-passwordless-sudo
        # fallback into ~/.local/bin, which is writable in a test.
        p = stub_bin / name
        p.write_text("#!/bin/sh\nexit 1\n")
        p.chmod(0o755)
    # mktemp -d is what install_gog uses for its workspace; force it somewhere we
    # can pre-seed, then drop the tarball in with the name its `find` looks for.
    work = tmp_path / "work"
    work.mkdir()
    mktemp = stub_bin / "mktemp"
    mktemp.write_text(f"#!/bin/sh\nprintf '%s' '{work}'\n")
    mktemp.chmod(0o755)
    (work / tarball.name).write_bytes(tarball.read_bytes())

    # bootstrap_agents.sh is not source-safe (sourcing it runs the whole bootstrap),
    # so slice out the REAL install_gog text and drive that. Still the shipped code,
    # not a paraphrase of it — a rewritten copy would pass while the box fails.
    fn = _extract_function(SCRIPT, "install_gog")
    script = (
        "log(){ :; }; ok(){ :; }; warn(){ echo \"WARN: $*\"; }; fail(){ echo \"FAIL: $*\"; }\n"
        f"{fn}\n"
        "install_gog\n"
    )
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True, text=True, timeout=60,
        env={"PATH": f"{stub_bin}:/usr/bin:/bin", "HOME": str(home)},
        cwd=str(tmp_path),
    )


# KEEP ALL THREE. The `./gog` case is the one that broke production, but it only
# FAILS the old code under GNU tar (Linux, and CI): bsdtar on macOS normalizes the
# leading `./` when matching a named member, so a dev running this locally sees it
# pass either way. The nested case fails everywhere, which is what makes the trio
# honest — do not prune it as redundant with `./gog`.
@pytest.mark.parametrize("member", [
    "gog",              # the layout that used to ship
    "./gog",            # gogcli 0.35.0 — what actually broke the rebuild (GNU tar only)
    "gogcli_9.9.9_linux_amd64/gog",   # a plausible next layout: nested in a dir
])
def test_install_gog_handles_every_tarball_layout(tmp_path, member):
    tgz = _make_tarball(tmp_path, member)
    res = _run_install_gog(tmp_path, tgz)
    installed = tmp_path / "home" / ".local" / "bin" / "gog"
    assert installed.exists(), (
        f"gog not installed from a tarball storing it at {member!r}\n"
        f"stdout: {res.stdout}\nstderr: {res.stderr}"
    )
    assert installed.stat().st_mode & 0o111, "installed gog is not executable"


def test_install_gog_fails_loudly_when_the_archive_has_no_binary(tmp_path):
    """A tarball with no gog at all must produce a NAMED failure, not a silent skip
    that leaves five agents mailless while the box reports healthy."""
    other = tmp_path / "readme.txt"
    other.write_text("not a binary\n")
    tgz = tmp_path / "gogcli_9.9.9_linux_amd64.tar.gz"
    with tarfile.open(tgz, "w:gz") as tf:
        tf.add(other, arcname="./README.txt")
    res = _run_install_gog(tmp_path, tgz)
    assert not (tmp_path / "home" / ".local" / "bin" / "gog").exists()
    assert "no executable 'gog'" in (res.stdout + res.stderr)
