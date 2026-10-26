"""capability.yaml (CapabilityManifest) and `canopy-runtime publish-capabilities`."""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
from canopy_runtime import CapabilityManifest, load_capability_manifest
from canopy_runtime.cli import main as cli_main
from canopy_runtime.publish import find_manifests, load_manifests
from pydantic import ValidationError

MANIFEST = """
version: 1
name: android-emulator
kind: ec2-ephemeral
description: Android 14 emulator with CommCare + Maestro, driven by the ace-mobile CLI.
aws:
  region: us-east-1
  stack: canopy-cap-ace-android-emulator
limits:
  max_concurrent: 4
  max_lease_s: 14400
  idle_release_s: 900
  ready_timeout_s: 900
params:
  state: {type: string, default: cc-2.63.0, enum: [cc-2.62.0, cc-2.63.0]}
exec:
  allow: [ace-mobile]
"""


def _manifest(**edits) -> dict:
    import yaml

    data = yaml.safe_load(MANIFEST)
    data.update(edits)
    return data


def test_the_spec_example_validates():
    m = load_capability_manifest(MANIFEST)
    assert (m.name, m.kind, m.aws.stack) == ("android-emulator", "ec2-ephemeral",
                                             "canopy-cap-ace-android-emulator")
    assert m.limits.max_concurrent == 4 and m.exec.allow == ["ace-mobile"]


@pytest.mark.parametrize("edits", [
    {"name": "AB"},                                  # too short, uppercase
    {"name": "x" * 41},
    {"kind": "fargate"},                             # the only kind v1 brokers
    {"version": 2},
    {"exec": {"allow": ["/usr/bin/ace-mobile"]}},    # a bare name, never a path
    {"exec": {"allow": ["ace mobile"]}},
    {"exec": {"allow": []}},
    {"surprise": True},                              # extra="forbid"
    {"limits": {"max_lease_s": 600, "idle_release_s": 900}},
    {"params": {"state": {"type": "string", "default": "cc-9", "enum": ["cc-2.63.0"]}}},
    {"params": {"Bad-Name": {"type": "string"}}},
    {"aws": {"region": "us-east-1"}},
])
def test_invalid_manifests_are_refused(edits):
    with pytest.raises(ValidationError):
        CapabilityManifest.model_validate(_manifest(**edits))


def test_params_are_checked_and_defaulted():
    m = CapabilityManifest.model_validate(_manifest(params={
        "state": {"type": "string", "default": "cc-2.63.0", "enum": ["cc-2.62.0", "cc-2.63.0"]},
        "cpus": {"type": "integer", "default": 2},
        "fast": {"type": "boolean"},
    }))
    assert m.resolve_params({"fast": True}) == {"state": "cc-2.63.0", "cpus": 2, "fast": True}
    with pytest.raises(ValueError) as e:
        m.resolve_params({"state": "cc-1", "cpus": True, "nope": 1})
    msg = str(e.value)
    assert "unknown param 'nope'" in msg and "'state'" in msg and "'cpus'" in msg
    assert "param 'fast' is required" in msg


def _repo(tmp_path: Path, manifests: dict[str, str]) -> Path:
    """Write manifests under tmp_path; returns the `infra/capabilities`-style root
    (tmp_path itself, since the keys carry their own subdirectory)."""
    for rel, body in manifests.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)
    return tmp_path


def _args(root: Path, rels) -> list[str]:
    return [a for rel in rels for a in ("--manifest", str(root / rel))]


def test_dir_finds_every_capability_manifest_one_level_down(tmp_path):
    """The failure this guards is silent and expensive: a publish that misses a
    manifest RETIRES that capability and releases its live leases. ACE has two."""
    root = _repo(tmp_path, {"android-emulator/capability.yaml": MANIFEST,
                            "test-worker/capability.yaml": POOL,
                            "android-emulator/deploy.sh": "#!/bin/sh\n",
                            "README.md": "not a capability dir\n"})
    assert [p.parent.name for p in find_manifests(root)] == ["android-emulator", "test-worker"]
    assert [m.name for m in load_manifests(find_manifests(root))] == ["android-emulator", "test-worker"]


def test_dir_refuses_an_empty_or_missing_directory(tmp_path):
    with pytest.raises(ValueError, match="would retire every capability"):
        find_manifests(_repo(tmp_path / "empty", {"README.md": "x"}))
    with pytest.raises(ValueError, match="not a directory"):
        find_manifests(tmp_path / "nope")


def test_dir_refuses_a_misnamed_manifest_it_would_skip(tmp_path):
    root = _repo(tmp_path, {"a/capability.yaml": MANIFEST, "b/capability.yml": POOL})
    with pytest.raises(ValueError, match="named capability.yaml"):
        find_manifests(root)


def test_a_duplicate_capability_name_is_refused(tmp_path):
    root = _repo(tmp_path, {"a/capability.yaml": MANIFEST, "b/capability.yaml": MANIFEST})
    with pytest.raises(ValueError, match="already declared"):
        load_manifests(find_manifests(root))


def test_an_invalid_manifest_exits_2_naming_the_file(tmp_path, capsys):
    root = _repo(tmp_path, {"bad/capability.yaml": "version: 1\nname: x\n"})
    assert cli_main(["publish-capabilities", "--dir", str(root), "--agent", "ace",
                     "--token", "t"]) == 2
    assert "bad/capability.yaml" in capsys.readouterr().err


def test_spec_is_retired_and_fails_loudly_with_the_new_usage(tmp_path, canopy, capsys):
    base, handler = canopy
    assert cli_main(["publish-capabilities", "--spec", "spec.yaml", "--agent", "ace",
                     "--base-url", base, "--token", "t"]) == 2
    err = capsys.readouterr().err
    assert "--spec is retired" in err and "--dir infra/capabilities" in err
    assert handler.seen == []  # nothing was sent: a shim here would have published


def test_naming_no_manifests_is_an_error_not_an_empty_publish(canopy, capsys):
    base, handler = canopy
    assert cli_main(["publish-capabilities", "--agent", "ace", "--base-url", base, "--token", "t"]) == 2
    assert "--dir infra/capabilities" in capsys.readouterr().err
    assert handler.seen == []


def test_the_same_file_via_dir_and_manifest_is_one_capability(tmp_path, canopy):
    base, handler = canopy
    handler.reply = (200, {"capabilities": []})
    root = _repo(tmp_path, {"android-emulator/capability.yaml": MANIFEST})
    assert cli_main(["publish-capabilities", "--dir", str(root), *_args(root, ["android-emulator/capability.yaml"]),
                     "--agent", "ace", "--base-url", base, "--token", "t"]) == 0
    assert [c["name"] for c in handler.seen[0][2]["capabilities"]] == ["android-emulator"]


class _Canopy(BaseHTTPRequestHandler):
    seen: list = []
    reply: tuple = (200, {})

    def do_PUT(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).seen.append((self.path, self.headers["Authorization"], body))
        status, resp = type(self).reply
        raw = json.dumps(resp).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *a):
        pass


@pytest.fixture()
def canopy():
    _Canopy.seen = []
    server = HTTPServer(("127.0.0.1", 0), _Canopy)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}", _Canopy
    server.shutdown()


def test_publish_puts_every_manifest_to_the_agents_route(tmp_path, canopy, capsys):
    base, handler = canopy
    handler.reply = (200, {"agent": "ace", "retired": ["old-cap"], "capabilities": [
        {"name": "android-emulator", "kind": "ec2-ephemeral",
         "stack": "canopy-cap-ace-android-emulator", "grant": None}]})
    root = _repo(tmp_path, {"android-emulator/capability.yaml": MANIFEST})
    rc = cli_main(["publish-capabilities", "--dir", str(root), "--agent", "ace",
                   "--base-url", base, "--token", "cpat_x"])
    assert rc == 0
    ((path, auth, body),) = handler.seen
    assert path == "/api/agents/ace/capabilities" and auth == "Bearer cpat_x"
    assert [c["name"] for c in body["capabilities"]] == ["android-emulator"]
    assert body["capabilities"][0]["exec"] == {"allow": ["ace-mobile"]}
    out = capsys.readouterr().out
    assert "NOT granted" in out and "retired ace/old-cap" in out


def test_a_refusal_exits_1_with_canopys_reason(tmp_path, canopy, capsys):
    base, handler = canopy
    handler.reply = (422, {"detail": "stack canopy-cap-ace-android-emulator has no LaunchTemplateId output"})
    root = _repo(tmp_path, {"c/capability.yaml": MANIFEST})
    assert cli_main(["publish-capabilities", "--dir", str(root), "--agent", "ace",
                     "--base-url", base, "--token", "t"]) == 1
    assert "LaunchTemplateId" in capsys.readouterr().err


def test_dry_run_sends_nothing(tmp_path, canopy, capsys):
    base, handler = canopy
    root = _repo(tmp_path, {"c/capability.yaml": MANIFEST})
    assert cli_main(["publish-capabilities", "--dir", str(root), "--agent", "ace",
                     "--base-url", base, "--dry-run"]) == 0
    assert handler.seen == []
    assert json.loads(capsys.readouterr().out)["capabilities"][0]["name"] == "android-emulator"


# ---- kind: pool --------------------------------------------------------------

POOL = """
version: 1
name: test-worker
kind: pool
description: Connect test workers, one device session each.
slots: [worker-1, worker-2, worker-3, worker-4]
limits: {max_lease_s: 14400, idle_release_s: 900}
"""


def _pool(**edits) -> dict:
    import yaml

    data = yaml.safe_load(POOL)
    data.update(edits)
    return data


def test_a_pool_validates_and_its_concurrency_is_its_slot_count():
    m = load_capability_manifest(POOL)
    assert (m.kind, m.slots, m.aws, m.exec) == ("pool", ["worker-1", "worker-2", "worker-3", "worker-4"],
                                                None, None)
    assert m.limits.max_concurrent == 4  # not the default 1: len(slots)
    # Round-trips: what canopy-web stores re-validates to the same manifest.
    assert CapabilityManifest.model_validate(m.model_dump(mode="json")) == m


def test_a_pool_manifest_may_only_lower_its_concurrency():
    lowered = CapabilityManifest.model_validate(_pool(limits={"max_concurrent": 2}))
    assert lowered.limits.max_concurrent == 2
    with pytest.raises(ValidationError, match="more than the pool's 4 slots"):
        CapabilityManifest.model_validate(_pool(limits={"max_concurrent": 5}))


@pytest.mark.parametrize("edits", [
    {"slots": []},
    {"slots": None},
    {"slots": ["worker-1", "worker-1"]},                       # unique
    {"slots": ["Worker 1"]},                                   # the capability-name pattern
    {"slots": ["w1"]},                                         # too short for it
    {"slots": [f"worker-{i}" for i in range(51)]},             # at most 50
    {"aws": {"region": "us-east-1", "stack": "canopy-cap-x"}},  # no machine, no stack
    {"exec": {"allow": ["ace-mobile"]}},
    {"params": {"slot": {"type": "string"}}},                  # the broker owns `slot`
    {"kind": "lottery"},
])
def test_invalid_pools_are_refused(edits):
    with pytest.raises(ValidationError):
        CapabilityManifest.model_validate(_pool(**edits))


@pytest.mark.parametrize("edits", [
    {"aws": None},
    {"exec": None},
    {"slots": ["worker-1"]},  # slots are for a pool
])
def test_an_ec2_ephemeral_manifest_keeps_its_shape(edits):
    data = _manifest()
    for k, v in edits.items():
        if v is None:
            data.pop(k)
        else:
            data[k] = v
    with pytest.raises(ValidationError):
        CapabilityManifest.model_validate(data)


def test_a_pool_acquire_takes_only_an_optional_declared_slot():
    m = load_capability_manifest(POOL)
    assert m.resolve_params(None) == {} and m.resolve_params({}) == {}
    assert m.resolve_params({"slot": "worker-3"}) == {"slot": "worker-3"}
    with pytest.raises(ValueError, match="not one of this pool's slots"):
        m.resolve_params({"slot": "worker-9"})
    with pytest.raises(ValueError, match="not one of this pool's slots"):
        m.resolve_params({"slot": 3})
    with pytest.raises(ValueError, match="takes only 'slot'"):
        m.resolve_params({"state": "x"})


def test_an_ec2_publish_body_carries_no_pool_keys(tmp_path):
    """An older canopy-web forbids unknown manifest keys: an ec2-ephemeral body
    must not grow `slots: null`."""
    from canopy_runtime.publish import payload

    root = _repo(tmp_path, {"e/capability.yaml": MANIFEST, "p/capability.yaml": POOL})
    ec2, pool = payload(load_manifests(find_manifests(root)))["capabilities"]
    assert "slots" not in ec2 and ec2["aws"]["stack"] == "canopy-cap-ace-android-emulator"
    assert "aws" not in pool and "exec" not in pool and pool["slots"][0] == "worker-1"
    assert CapabilityManifest.model_validate(ec2) and CapabilityManifest.model_validate(pool)
