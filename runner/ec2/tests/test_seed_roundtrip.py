"""The first-boot seed must fit Secrets Manager AND decode on the box, byte for byte.

On 2026-10-04 `up.sh` could not build a runner at all: it published
cloud_runner.py as ONE gzip+base64 secret, the file had grown to 210 KB (91 KB
encoded), and Secrets Manager caps a value at 65536 bytes — PutSecretValue
failed on every run. The seed is now a manifest plus parts (seed.py).

What has to hold, and what these pin:
  * every value up.sh writes is under the cap, for the REAL cloud_runner.py;
  * the decoder that actually runs on the box — the shell in runner.cfn.yaml's
    canopy-fetch-env, not seed.py's Python — reproduces the file exactly and
    stamps its provenance, and refuses a seed whose parts do not match;
  * update_runner.sh --from-secret reads the same format;
  * the rendered UserData stays under EC2's 16 KB cap (the cap that pushed the
    code into Secrets Manager in the first place);
  * up.sh stamps the seed with the same path-scoped sha the deploy expects.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import re
import subprocess
import sys

import pytest

EC2 = pathlib.Path(__file__).resolve().parent.parent
TEMPLATE = EC2 / "runner.cfn.yaml"
SM_CAP = 65536
SEED_ID = "canopy/cloud-runner/runner-seed"


def _seed_mod():
    spec = importlib.util.spec_from_file_location("seed", EC2 / "seed.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["seed"] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def test_every_published_value_fits_secrets_manager():
    seed = _seed_mod()
    data = (EC2 / "cloud_runner.py").read_bytes()
    manifest, parts = seed.build(data, sha="a" * 40, committed_at=1)
    assert len(json.dumps(manifest).encode()) < SM_CAP
    assert all(len(p.encode()) < SM_CAP for p in parts), [len(p) for p in parts]
    assert manifest["parts"] == len(parts)
    assert seed.assemble(manifest, parts) == data


def test_the_seed_scales_past_one_value():
    """The bug was a hard ceiling. A file several times today's size still seeds."""
    seed = _seed_mod()
    big = (EC2 / "cloud_runner.py").read_bytes() * 5 + b"\n# " + bytes(range(256)) * 400
    manifest, parts = seed.build(big)
    assert len(parts) > 1
    assert all(len(p) <= seed.PART_SIZE for p in parts)
    assert seed.assemble(manifest, parts) == big


def test_encoding_is_deterministic():
    """Same bytes, same parts — so re-running up.sh with no code change rewrites
    identical values rather than churning every secret."""
    seed = _seed_mod()
    data = b"print('x')\n" * 1000
    assert seed.build(data) == seed.build(data)


# --- the decoder that runs on the box ----------------------------------------

def _fetch_env_seed_block() -> str:
    """The seed-install `if ... fi` out of canopy-fetch-env, de-indented the way
    YAML's block scalar does it, with CloudFormation's !Sub applied."""
    text = TEMPLATE.read_text()
    start = text.index("                if [ ! -s /opt/canopy-runner/cloud_runner.py ]; then\n")
    end = text.index("\n                fi\n", start) + len("\n                fi\n")
    block = "\n".join(line[16:] if line.startswith(" " * 16) else line.lstrip(" ")
                      for line in text[start:end].splitlines())
    return block.replace("${RunnerSeedSecret}", SEED_ID).replace("${!", "${")


def _run_box_decoder(tmp_path, secrets: dict[str, str]) -> subprocess.CompletedProcess:
    store = tmp_path / "sm"
    store.mkdir()
    for sid, val in secrets.items():
        (store / sid.replace("/", "__")).write_text(val + "\n")  # --output text adds one
    home = tmp_path / "opt-canopy-runner"
    home.mkdir()
    script = (
        "set -euo pipefail\n"
        f'secret() {{ cat "{store}/$(printf %s "$1" | sed "s#/#__#g")"; }}\n'
        "chown() { :; }\n"
        + _fetch_env_seed_block().replace("/opt/canopy-runner", str(home))
    )
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60)


def _published(data: bytes, *, part_size: int, sha="b" * 40, committed_at=1700000000):
    seed = _seed_mod()
    manifest, parts = seed.build(data, sha=sha, committed_at=committed_at, size=part_size)
    secrets = {SEED_ID: json.dumps(manifest)}
    secrets.update({f"{SEED_ID}-part-{i}": p for i, p in enumerate(parts)})
    return manifest, secrets


@pytest.mark.parametrize("part_size", [60000, 4096])
def test_the_box_reassembles_the_real_runner(tmp_path, part_size):
    data = (EC2 / "cloud_runner.py").read_bytes()
    manifest, secrets = _published(data, part_size=part_size)
    res = _run_box_decoder(tmp_path, secrets)
    assert res.returncode == 0, res.stderr
    home = tmp_path / "opt-canopy-runner"
    assert (home / "cloud_runner.py").read_bytes() == data
    stamp = json.loads((home / "build-info.json").read_text())
    assert stamp == {"sha": "b" * 40, "committed_at": 1700000000, "ref": "seed"}
    assert not (home / "seed").exists(), "the staging dir must not be left behind"


def test_the_box_refuses_parts_that_do_not_match_the_manifest(tmp_path):
    """A box booting while up.sh is mid-publish sees a new manifest over old
    parts. It must fail the start (systemd retries), never install mixed bytes."""
    data = (EC2 / "cloud_runner.py").read_bytes()
    _, secrets = _published(data, part_size=4096)
    new_manifest, _ = _published(data + b"\n# newer\n", part_size=4096)
    secrets[SEED_ID] = json.dumps(new_manifest)
    res = _run_box_decoder(tmp_path, secrets)
    assert res.returncode != 0
    assert "sha256 mismatch" in res.stderr
    assert not (tmp_path / "opt-canopy-runner" / "cloud_runner.py").exists()


def test_userdata_stays_under_the_ec2_cap():
    """Rendered with realistic (long) parameter values, raw — EC2 measures the
    user data before base64."""
    text = TEMPLATE.read_text()
    start = text.index("        Fn::Base64: !Sub |\n") + len("        Fn::Base64: !Sub |\n")
    end = text.index("\nOutputs:", start)
    body = "\n".join(line[10:] for line in text[start:end].splitlines())
    rendered = re.sub(r"\$\{(?!!)[A-Za-z:]+\}", "x" * 60, body).replace("${!", "${")
    assert len(rendered.encode()) < 16384 - 1024, (
        f"UserData is {len(rendered.encode())} bytes; EC2's hard cap is 16384")


# --- update_runner.sh --from-secret ------------------------------------------

def test_from_secret_installs_the_manifest_seed_with_its_provenance(tmp_path):
    data = b"#!/usr/bin/env python3\nprint('seeded')\n"
    _, secrets = _published(data, part_size=16, sha="c" * 40, committed_at=42)
    store = tmp_path / "sm"
    store.mkdir()
    for sid, val in secrets.items():
        (store / sid.replace("/", "__")).write_text(val + "\n")
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    (bin_ / "aws").write_text(
        "#!/usr/bin/env python3\n"
        "import sys, pathlib\n"
        "a = sys.argv[1:]\n"
        "sid = a[a.index('--secret-id') + 1]\n"
        f"p = pathlib.Path({str(store)!r}) / sid.replace('/', '__')\n"
        "sys.exit(1) if not p.exists() else print(p.read_text(), end='')\n"
    )
    (bin_ / "aws").chmod(0o755)
    (bin_ / "fake-restart").write_text("#!/bin/sh\nexit 0\n")
    (bin_ / "fake-restart").chmod(0o755)
    home = tmp_path / "home"
    home.mkdir()
    res = subprocess.run(
        ["bash", str(EC2 / "update_runner.sh"), "--from-secret"],
        capture_output=True, text=True, timeout=60,
        env={"PATH": f"{bin_}:/usr/bin:/bin", "HOME": str(tmp_path),
             "RUNNER_HOME": str(home), "RESTART_CMD": str(bin_ / "fake-restart")},
    )
    assert res.returncode == 0, res.stdout + res.stderr
    assert (home / "cloud_runner.py").read_bytes() == data
    stamp = json.loads((home / "build-info.json").read_text())
    assert stamp["sha"] == "c" * 40 and stamp["committed_at"] == 42


# --- provenance -----------------------------------------------------------------

def _paths(text: str, pattern: str) -> list[str]:
    m = re.search(pattern, text, re.M)
    assert m, pattern
    return sorted(pathlib.Path(p.strip('"')).name for p in m.group(1).split())


def test_up_sh_stamps_the_seed_over_the_same_paths_the_deploy_does():
    up = _paths((EC2 / "up.sh").read_text(), r"^SHA_PATHS=\((.*)\)$")
    workflow = (EC2.parent.parent / ".github" / "workflows" / "deploy-labs.yml").read_text()
    deploy = _paths(workflow, r'PATHS="([^"]+)"')
    assert up == deploy, (up, deploy)
