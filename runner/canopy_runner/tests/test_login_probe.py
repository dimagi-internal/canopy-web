"""A laptop box marked signed out re-tests its Claude login directly.

Before login_probe, nothing did: the mark cleared only when a stray session's Stop
hook saw a real reply, or after the 15-minute marker TTL — and a signed-out box
claims no turns, so no session arrives. jj-mbp-cdp stayed ready:false for ~40
minutes after its owner signed back in (2026-11-20).
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from canopy_runner import desktop, hooks, login_probe, readiness


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setattr(readiness.cdp_control, "cdp_healthy", lambda **kw: True)
    monkeypatch.setattr(desktop, "claude_cli", lambda: Path("/usr/bin/claude"))
    return SimpleNamespace(state_path=str(tmp_path / "runner-state.json"), cdp_port=9222, runner_id="r1")


def _answer(monkeypatch, status, detail=""):
    calls = []

    def run(cli):
        calls.append(cli)
        return status, detail
    monkeypatch.setattr(login_probe, "run_probe", run)
    return calls


def test_a_signed_out_box_whose_probe_answers_is_ready_on_the_next_compute(cfg, monkeypatch):
    readiness.mark_signed_out(cfg)
    calls = _answer(monkeypatch, "ok", "answered a prompt")
    assert readiness.compute(cfg) == (True, "")
    assert len(calls) == 1
    assert not readiness.is_signed_out(cfg)
    assert login_probe.health_check(cfg)["status"] == "ok"


def test_a_probe_that_says_not_logged_in_stays_not_ready(cfg, monkeypatch):
    readiness.mark_signed_out(cfg)
    marker = readiness._marker(cfg)
    old = time.time() - 600
    os.utime(marker, (old, old))
    _answer(monkeypatch, "dead", "Login reached its time limit · Please run /login")
    ready, note = readiness.compute(cfg)
    assert ready is False and note.startswith("Claude is not signed in")
    # A probe is not a new failure: the TTL keeps counting from the real one.
    assert marker.stat().st_mtime == pytest.approx(old, abs=1)
    check = login_probe.health_check(cfg)
    assert check["name"] == "claude.probe.local" and check["status"] == "fail"
    assert "not signed in" in check["detail"]


def test_no_probe_runs_while_the_box_is_not_marked_signed_out(cfg, monkeypatch):
    calls = _answer(monkeypatch, "ok")
    assert readiness.compute(cfg) == (True, "")
    readiness.mark_failed(cfg, "some other failure")
    assert readiness.compute(cfg)[0] is False
    assert calls == []
    assert login_probe.health_check(cfg) is None


def test_probes_are_rate_limited(cfg, monkeypatch):
    readiness.mark_signed_out(cfg)
    calls = _answer(monkeypatch, "dead", "Not logged in · Please run /login")
    readiness.compute(cfg)
    readiness.compute(cfg)   # the claim gate computes again in the same tick
    assert len(calls) == 1
    later = time.time() + login_probe.PROBE_INTERVAL_SECONDS + 1
    login_probe.maybe_probe(cfg, now=later)
    assert len(calls) == 2


def test_no_cli_means_no_probe(cfg, monkeypatch):
    readiness.mark_signed_out(cfg)
    monkeypatch.setattr(desktop, "claude_cli", lambda: None)
    calls = _answer(monkeypatch, "ok")
    assert login_probe.maybe_probe(cfg) is None and calls == []


@pytest.mark.parametrize("rc, stdout, stderr, status", [
    (0, json.dumps({"result": "OK", "is_error": False}), "", "ok"),
    (1, json.dumps({"result": "Login reached its time limit · Please run /login", "is_error": True}), "", "dead"),
    (1, "", "Not logged in · Please run /login", "dead"),
    (1, json.dumps({"result": "API Error: 500", "is_error": True}), "", "unknown"),
    (0, "not json", "", "unknown"),
])
def test_classify(rc, stdout, stderr, status):
    assert login_probe.classify(rc, stdout, stderr)[0] == status


def test_the_probe_runs_the_cli_without_user_hooks_or_a_saved_session(monkeypatch, tmp_path):
    seen = {}

    def fake_run(argv, **kw):
        seen["argv"] = argv
        return SimpleNamespace(returncode=0, stdout=json.dumps({"result": "OK", "is_error": False}), stderr="")
    monkeypatch.setattr(login_probe.subprocess, "run", fake_run)
    assert login_probe.run_probe(tmp_path / "claude")[0] == "ok"
    argv = seen["argv"]
    # No user settings: the canopy Stop hook must never see a probe and re-mark the box.
    assert argv[argv.index("--setting-sources") + 1] == "project"
    assert "--no-session-persistence" in argv


def test_the_signed_out_alert_no_longer_claims_it_never_resets(cfg):
    posted = []
    client = SimpleNamespace(post_alert=posted.append)
    hooks.report_signed_out(cfg, client, "Login reached its time limit · Please run /login")
    summary = posted[0]["summary"]
    assert "does not reset on its own" not in summary
    assert "2 minutes" in summary
