"""capability.yaml (CapabilityManifest), runtime.yaml's `capabilities:`, and
`canopy-runtime publish-capabilities`."""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
from canopy_runtime import CapabilityManifest, load_capability_manifest, load_runtime_yaml
from canopy_runtime.cli import main as cli_main
from canopy_runtime.publish import load_manifests
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


def test_runtime_yaml_declares_capabilities_by_relative_path():
    spec = load_runtime_yaml("capabilities:\n  - manifest: infra/capabilities/x/capability.yaml\n")
    assert spec.capabilities[0].manifest == "infra/capabilities/x/capability.yaml"
    with pytest.raises(ValidationError):
        load_runtime_yaml("capabilities:\n  - manifest: /etc/capability.yaml\n")
    with pytest.raises(ValidationError):
        load_runtime_yaml("capabilities:\n  - path: x.yaml\n")


def _repo(tmp_path: Path, manifests: dict[str, str]) -> Path:
    for rel, body in manifests.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)
    spec = tmp_path / "runtime.yaml"
    spec.write_text("capabilities:\n" + "".join(f"  - manifest: {rel}\n" for rel in manifests))
    return spec


def test_manifest_paths_resolve_relative_to_the_spec_file(tmp_path, monkeypatch):
    spec = _repo(tmp_path / "repo", {"infra/capabilities/android-emulator/capability.yaml": MANIFEST})
    monkeypatch.chdir(tmp_path)  # NOT the repo: the spec file's dir is what counts
    (m,) = load_manifests(Path("repo/runtime.yaml"))
    assert m.name == "android-emulator"
    assert spec.exists()


def test_a_duplicate_capability_name_is_refused(tmp_path):
    spec = _repo(tmp_path, {"a/capability.yaml": MANIFEST, "b/capability.yaml": MANIFEST})
    with pytest.raises(ValueError, match="already declared"):
        load_manifests(spec)


def test_an_invalid_manifest_exits_2_naming_the_file(tmp_path, capsys):
    spec = _repo(tmp_path, {"bad/capability.yaml": "version: 1\nname: x\n"})
    assert cli_main(["publish-capabilities", "--spec", str(spec), "--agent", "ace",
                     "--token", "t"]) == 2
    assert "bad/capability.yaml" in capsys.readouterr().err


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
    spec = _repo(tmp_path, {"infra/capabilities/android-emulator/capability.yaml": MANIFEST})
    rc = cli_main(["publish-capabilities", "--spec", str(spec), "--agent", "ace",
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
    spec = _repo(tmp_path, {"c/capability.yaml": MANIFEST})
    assert cli_main(["publish-capabilities", "--spec", str(spec), "--agent", "ace",
                     "--base-url", base, "--token", "t"]) == 1
    assert "LaunchTemplateId" in capsys.readouterr().err


def test_dry_run_sends_nothing(tmp_path, canopy, capsys):
    base, handler = canopy
    spec = _repo(tmp_path, {"c/capability.yaml": MANIFEST})
    assert cli_main(["publish-capabilities", "--spec", str(spec), "--agent", "ace",
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

    spec = _repo(tmp_path, {"e/capability.yaml": MANIFEST, "p/capability.yaml": POOL})
    ec2, pool = payload(load_manifests(spec))["capabilities"]
    assert "slots" not in ec2 and ec2["aws"]["stack"] == "canopy-cap-ace-android-emulator"
    assert "aws" not in pool and "exec" not in pool and pool["slots"][0] == "worker-1"
    assert CapabilityManifest.model_validate(ec2) and CapabilityManifest.model_validate(pool)
