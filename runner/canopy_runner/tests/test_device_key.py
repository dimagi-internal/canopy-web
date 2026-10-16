"""Runner start enrolls this machine's device key (Canopy Vault, dimagi-internal/canopy#946).

The key and its registration are canopy's (`canopy vault device --enroll`); the runner runs
it once per start as ITS identity, says what a person must do while the key is pending (log
+ one notification per key), and never fails or blocks over it."""
from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace

import pytest

from canopy_runner import device_key as dk

CFG = SimpleNamespace(base_url="https://canopy.example/", token="RUNNER_PAT_SECRET",
                      runner_id="r-123")


class Fake:
    def __init__(self, *, rc=0, out=None, err=""):
        self.rc, self.err, self.calls = rc, err, []
        self.out = out if out is not None else json.dumps(
            {"status": "pending", "jkt": "JKT-1", "kind": "secure-enclave",
             "approve_url": "https://canopy.example/approve/4", "created": True})

    def __call__(self, argv, **kw):
        self.calls.append((argv, kw))
        if argv[0] == "osascript":
            return subprocess.CompletedProcess(argv, 0, "", "")
        return subprocess.CompletedProcess(argv, self.rc, self.out, self.err)


def _enroll(tmp_path, fake, monkeypatch, platform="darwin"):
    monkeypatch.setattr(dk.sys, "platform", platform)
    return dk.enroll(CFG, runner=fake, home=tmp_path, cli="/x/canopy")


def test_enrolls_as_this_runner_and_never_as_an_agent(tmp_path, monkeypatch):
    monkeypatch.setenv("CANOPY_AGENT", "eva")
    monkeypatch.setenv("CANOPY_TURN_ID", "t-1")
    fake = Fake()
    st = _enroll(tmp_path, fake, monkeypatch)
    argv, kw = fake.calls[0]
    assert argv == ["/x/canopy", "vault", "device", "--enroll", "--runner", "r-123", "--json"]
    assert kw["env"]["CANOPY_WEB_PAT"] == "RUNNER_PAT_SECRET"
    assert kw["env"]["CANOPY_WEB_API_URL"] == "https://canopy.example"
    assert "CANOPY_AGENT" not in kw["env"] and "CANOPY_TURN_ID" not in kw["env"]
    assert st["status"] == "pending"


def test_pending_logs_the_approve_url_and_notifies_once_per_key(tmp_path, monkeypatch, caplog):
    fake = Fake()
    with caplog.at_level("INFO"):
        _enroll(tmp_path, fake, monkeypatch)
    assert "https://canopy.example/approve/4" in caplog.text
    assert "RUNNER_PAT_SECRET" not in caplog.text
    assert sum(1 for a, _ in fake.calls if a[0] == "osascript") == 1
    _enroll(tmp_path, fake, monkeypatch)                     # the next restart
    assert sum(1 for a, _ in fake.calls if a[0] == "osascript") == 1


def test_an_active_key_is_quiet(tmp_path, monkeypatch):
    fake = Fake(out=json.dumps({"status": "active", "jkt": "J", "kind": "software"}))
    _enroll(tmp_path, fake, monkeypatch)
    assert not [a for a, _ in fake.calls if a[0] == "osascript"]


@pytest.mark.parametrize("fake,why", [
    (Fake(rc=2, out="", err="Error: No such command 'vault'."), "predates"),
    (Fake(rc=1, out="", err="canopy-web refused POST /api/runner-device-keys/ -> 404"), "404"),
    (Fake(out="not json"), "no status"),
])
def test_every_failure_is_one_line_and_never_raises(tmp_path, monkeypatch, fake, why):
    assert why in _enroll(tmp_path, fake, monkeypatch)["error"]


def test_no_canopy_cli_or_opt_out_does_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(dk, "canopy_cli", lambda home=None: None)
    assert dk.enroll(CFG, runner=Fake(), home=tmp_path) == {"error": "no canopy CLI"}
    monkeypatch.setenv("CANOPY_DEVICE_KEY", "0")
    fake = Fake()
    assert "skipped" in dk.enroll(CFG, runner=fake, home=tmp_path, cli="/x/canopy")
    assert fake.calls == []


def test_start_runs_in_the_background_and_swallows_errors(monkeypatch):
    def boom(cfg, **kw):
        raise RuntimeError("no")

    monkeypatch.setattr(dk, "enroll", boom)
    t = dk.start(CFG)
    t.join(5)
    assert not t.is_alive()


def test_finds_the_windows_canopy_exe(tmp_path, monkeypatch):
    # A Windows runner (TPM key, canopy-web#1549): `uv tool install` leaves canopy.exe.
    monkeypatch.setattr(dk.shutil, "which", lambda _: None)
    exe = tmp_path / ".local" / "bin" / "canopy.exe"
    exe.parent.mkdir(parents=True)
    exe.write_text("")
    exe.chmod(0o755)
    assert dk.canopy_cli(tmp_path) == str(exe)


def test_a_new_ecdh_key_sending_the_key_back_to_pending_notifies_again(tmp_path, monkeypatch):
    """canopy-web#1550: registering the ECDH key on an approved key resets it to pending —
    same signing thumbprint, so a marker keyed by jkt alone would stay silent."""
    def pending(ecdh_jkt):
        return Fake(out=json.dumps({"status": "pending", "jkt": "JKT-1", "kind": "secure-enclave",
                                    "approve_url": "https://canopy.example/approve/4",
                                    "ecdh_jkt": ecdh_jkt, "ecdh_kind": "secure-enclave"}))

    notes = lambda f: sum(1 for a, _ in f.calls if a[0] == "osascript")   # noqa: E731
    first = Fake()                                    # an older canopy: no ECDH key
    _enroll(tmp_path, first, monkeypatch)
    again = pending("ECDH-1")
    _enroll(tmp_path, again, monkeypatch)
    assert notes(first) == 1 and notes(again) == 1
    quiet = pending("ECDH-1")
    _enroll(tmp_path, quiet, monkeypatch)
    assert notes(quiet) == 0
