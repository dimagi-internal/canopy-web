"""canopy-aws-broker: one hour of a NAMED read-only role, for a runner uid only.

Pinned here: an unknown uid, an unvended role or a malformed agent gets an error and
NO STS call; the session is stamped with the runner (from the uid, unforgeable) and the
agent; keys are cached per (uid, role, agent); the STS request is a signed AssumeRole
whose XML answer becomes credential_process JSON; the one-line framing over a real
Unix socket; `--install` writes its unit; and the file stays stdlib-only (it runs on the
box's system python3, as root).
"""
from __future__ import annotations

import ast
import importlib.util
import io
import json
import os
import pathlib
import socket
import sys
import tempfile
import threading
import urllib.parse

import pytest

_DIR = pathlib.Path(__file__).resolve().parent.parent
_PATH = _DIR / "aws_broker.py"

RUNNERS = {1001: {"id": "dimagi", "name": "cloud-ec2-2", "uid": 1001}}
ROLES = {"labs-observe": "labs-jj-observe-readonly"}


@pytest.fixture
def ab():
    spec = importlib.util.spec_from_file_location("aws_broker", _PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


class FakeSts:
    def __init__(self):
        self.calls = []

    def role_arn(self, role):
        return f"arn:aws:iam::858923557655:role/{role}"

    def assume(self, role_arn, **kw):
        self.calls.append({"role_arn": role_arn, **kw})
        return {"Version": 1, "AccessKeyId": "ASIA", "SecretAccessKey": "s", "SessionToken": "t",
                "Expiration": "2099-01-01T00:00:00Z", "RoleArn": role_arn}


def _ask(ab, sts, cache, uid=1001, **req):
    return ab.answer(json.dumps(req).encode(), uid, RUNNERS, sts, cache, role_map=ROLES)


def test_unknown_uid_unvended_role_and_bad_agent_never_reach_sts(ab):
    sts, cache = FakeSts(), ab.Cache()
    assert "not a runner" in _ask(ab, sts, cache, uid=0, role="labs-observe", agent="hal")["error"]
    assert "does not vend" in _ask(ab, sts, cache, role="labs-admin", agent="hal")["error"]
    assert "agent slug" in _ask(ab, sts, cache, role="labs-observe", agent="../root")["error"]
    assert sts.calls == []


def test_session_is_stamped_with_the_runner_and_agent_and_cached(ab):
    sts, cache = FakeSts(), ab.Cache()
    out = _ask(ab, sts, cache, role="labs-observe", agent="hal")
    assert out["AccessKeyId"] == "ASIA" and out["Version"] == 1
    (call,) = sts.calls
    assert call["role_arn"].endswith(":role/labs-jj-observe-readonly")
    assert call["source_identity"] == "canopy-runner-cloud-ec2-2"
    assert call["session_name"] == "cloud-ec2-2.hal"
    assert call["tags"] == {"runner": "cloud-ec2-2", "agent": "hal"}
    assert _ask(ab, sts, cache, role="labs-observe", agent="hal") == out
    assert len(sts.calls) == 1                      # served from the cache
    _ask(ab, sts, cache, role="labs-observe", agent="ace")
    assert len(sts.calls) == 2                      # another agent, its own session


def test_nearly_expired_keys_are_minted_again(ab):
    sts, cache = FakeSts(), ab.Cache()
    cache.put((1001, "labs-observe", "hal"), {"Expiration": "2000-01-01T00:00:00Z"})
    _ask(ab, sts, cache, role="labs-observe", agent="hal")
    assert len(sts.calls) == 1


_STS_XML = b"""<AssumeRoleResponse xmlns="https://sts.amazonaws.com/doc/2011-06-15/">
<AssumeRoleResult><Credentials><AccessKeyId>ASIAX</AccessKeyId><SecretAccessKey>sec</SecretAccessKey>
<SessionToken>tok</SessionToken><Expiration>2026-10-29T18:00:00Z</Expiration></Credentials>
</AssumeRoleResult></AssumeRoleResponse>"""


class _Creds:
    def get(self):
        return {"access_key": "AKIA", "secret_key": "x", "token": "itok", "expires_at": 9e9}


def test_sts_assume_role_is_signed_and_parsed(ab):
    seen = {}

    def opener(req, timeout=None):
        seen["url"], seen["body"], seen["headers"] = req.full_url, req.data.decode(), dict(req.headers)
        return io.BytesIO(_STS_XML)

    sts = ab.Sts(creds=_Creds(), opener=opener)
    out = sts.assume("arn:aws:iam::1:role/labs-jj-observe-readonly", session_name="b.hal",
                     source_identity="canopy-runner-b", tags={"agent": "hal", "runner": "b"})
    assert out == {"Version": 1, "AccessKeyId": "ASIAX", "SecretAccessKey": "sec",
                   "SessionToken": "tok", "Expiration": "2026-10-29T18:00:00Z",
                   "RoleArn": "arn:aws:iam::1:role/labs-jj-observe-readonly"}
    body = dict(urllib.parse.parse_qsl(seen["body"]))
    assert seen["url"] == "https://sts.us-east-1.amazonaws.com/"
    assert body["Action"] == "AssumeRole" and body["SourceIdentity"] == "canopy-runner-b"
    assert body["DurationSeconds"] == "3600"
    assert {body["Tags.member.1.Key"], body["Tags.member.2.Key"]} == {"agent", "runner"}
    assert seen["headers"]["Authorization"].startswith("AWS4-HMAC-SHA256 Credential=AKIA/")
    assert "/sts/aws4_request" in seen["headers"]["Authorization"]
    assert seen["headers"]["X-amz-security-token"] == "itok"


def test_one_line_over_a_real_unix_socket(ab):
    sts = FakeSts()
    with tempfile.TemporaryDirectory(dir="/tmp") as d:
        path = os.path.join(d, "s")
        server = ab.Broker(path, sts, runners_loader=lambda: RUNNERS, uid_of=lambda s: 1001)
        server.cache = ab.Cache()
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        try:
            os.environ["CANOPY_AWS_BROKER_ROLES"] = json.dumps(ROLES)
            c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            c.connect(path)
            c.sendall(b'{"role": "labs-observe", "agent": "hal"}\n')
            reply = json.loads(c.makefile().readline())
            c.close()
        finally:
            os.environ.pop("CANOPY_AWS_BROKER_ROLES", None)
            server.shutdown()
            server.server_close()
    assert reply["SessionToken"] == "t"


def test_install_writes_its_unit_and_starts_it(ab, tmp_path):
    calls = []
    unit = tmp_path / "canopy-aws-broker.service"
    assert ab.install(run=lambda argv, **kw: calls.append(argv), unit_path=unit) is True
    assert f"ExecStart=/usr/bin/python3 {_PATH}" in unit.read_text()
    assert ["systemctl", "enable", "--now", "canopy-aws-broker.service"] in calls
    calls.clear()
    assert ab.install(run=lambda argv, **kw: calls.append(argv), unit_path=unit) is False
    assert ["systemctl", "restart", "canopy-aws-broker.service"] not in calls


def test_stdlib_only():
    tree = ast.parse(_PATH.read_text())
    mods = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            mods.add(node.module.split(".")[0])
    assert mods - set(sys.stdlib_module_names) == {"kms_broker"}
