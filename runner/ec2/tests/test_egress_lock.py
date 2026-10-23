"""The egress lock (`RUNNER_EGRESS=locked`): a unit reaches the network only through
the local CONNECT proxy, to an allowlist of hosts; root's CANOPY-EGRESS chain rejects
the rest, DNS included. A runner setting of its own — any box may lock — which ZDR
mode requires.

docs/superpowers/specs/2026-10-22-phi-agents-on-zdr-runners-design.md §1.
"""
from __future__ import annotations

import asyncio
import json
import pathlib
import subprocess

import pytest

BOX = {"RUNNER_NAME": "cloud-zdr-1", "RUNNER_WORKSPACE": "dimagi",
       "CANOPY_BASE_URL": "https://canopy.dimagi.com", "RUNNER_UNITS": "tree:dimagi"}
UNITS = [{"id": "dimagi", "uid": 1001, "gid": 1001, "name": "cloud-zdr-1", "kind": "tree", "slug": "",
          "workspace": "dimagi", "user": "cu-dimagi", "home": "/home/cu-dimagi"},
         {"id": "agent-eva", "uid": 1002, "gid": 1002, "name": "cloud-zdr-1-agent-eva", "kind": "agent",
          "slug": "eva", "workspace": "dimagi", "user": "cu-agent-eva", "home": "/home/cu-agent-eva"}]


class Recorder:
    """A fake `subprocess.run` that records argv and answers per a rule."""

    def __init__(self, fail=lambda argv: False, missing=lambda argv: False):
        self.calls, self._fail, self._missing = [], fail, missing

    def __call__(self, argv, **kw):
        self.calls.append(list(argv))
        if self._fail(argv):
            return subprocess.CompletedProcess(argv, 1, "", "denied")
        return subprocess.CompletedProcess(argv, 1 if self._missing(argv) else 0, "", "")

    def appended(self, tool="iptables"):
        return [c[3:] for c in self.calls if c[:3] == [tool, "-A", "CANOPY-EGRESS"]]


# ---- the allowlist ------------------------------------------------------------------

def test_the_allowlist_is_the_base_canopy_and_the_boxs_own(cloud_runner):
    allow = cloud_runner.egress_allowlist({**BOX, "RUNNER_EGRESS_ALLOW": " Slack.com, .googleapis.com ,"})
    assert "api.anthropic.com" in allow and "github.com" in allow
    assert "canopy.dimagi.com" in allow
    assert "slack.com" in allow and ".googleapis.com" in allow


def test_an_allowlist_entry_that_is_not_a_hostname_is_refused(cloud_runner):
    for bad in ("evil.com/path", "*.example.com", "1.2.3.4:443", "a b"):
        with pytest.raises(ValueError):
            cloud_runner.egress_allowlist({**BOX, "RUNNER_EGRESS_ALLOW": bad})


def test_a_suffix_admits_subdomains_only(cloud_runner):
    allow = [".googleapis.com", "github.com"]
    assert cloud_runner.egress_host_allowed("sheets.googleapis.com", allow)
    assert cloud_runner.egress_host_allowed("GitHub.com.", allow)
    assert not cloud_runner.egress_host_allowed("googleapis.com.evil.io", allow)
    assert not cloud_runner.egress_host_allowed("notgithub.com", allow)
    assert not cloud_runner.egress_host_allowed("gist.github.com", allow)


def test_any_value_locks_a_typo_fails_closed(cloud_runner):
    assert cloud_runner.egress_locked({"RUNNER_EGRESS": "locked"})
    assert cloud_runner.egress_locked({"RUNNER_EGRESS": "lockd"})
    assert not cloud_runner.egress_locked({"RUNNER_EGRESS": " "})
    assert not cloud_runner.egress_locked({})


# ---- root's rules -------------------------------------------------------------------

def test_locked_each_unit_may_send_only_on_loopback_and_not_dns(cloud_runner, monkeypatch):
    monkeypatch.setattr(cloud_runner.shutil, "which", lambda t: None)    # no ip6tables
    run = Recorder(missing=lambda argv: argv[1] == "-C")
    assert cloud_runner.egress_rules(UNITS, locked=True, run=run) == ["iptables"]
    assert ["iptables", "-F", "CANOPY-EGRESS"] in run.calls
    assert ["iptables", "-I", "OUTPUT", "1", "-j", "CANOPY-EGRESS"] in run.calls
    rules = run.appended()
    own = ["-m", "owner", "--uid-owner", "1001"]
    assert rules[:5] == [[*own, "-o", "lo", "-p", "udp", "--dport", "53", "-j", "REJECT"],
                         [*own, "-o", "lo", "-p", "tcp", "--dport", "53", "-j", "REJECT"],
                         # not the other unit's proxy: that is the other unit's allowlist
                         [*own, "-o", "lo", "-p", "tcp", "--dport", "21002", "-j", "REJECT"],
                         [*own, "-o", "lo", "-j", "RETURN"],
                         [*own, "-j", "REJECT"]]
    assert rules[9] == ["-m", "owner", "--uid-owner", "1002", "-j", "REJECT"]


def test_only_the_named_units_are_locked(cloud_runner, monkeypatch):
    monkeypatch.setattr(cloud_runner.shutil, "which", lambda t: None)
    run = Recorder()
    cloud_runner.egress_rules(UNITS, locked={"agent-eva"}, run=run)
    owners = {r[3] for r in run.appended()}
    assert owners == {"1002"}


def test_each_unit_has_its_own_stable_port(cloud_runner):
    assert cloud_runner.egress_port(1001) == 21001
    assert cloud_runner.egress_port(1002) == 21002


def test_unlocked_the_chain_is_emptied(cloud_runner, monkeypatch):
    monkeypatch.setattr(cloud_runner.shutil, "which", lambda t: "/sbin/ip6tables")
    run = Recorder()
    assert cloud_runner.egress_rules(UNITS, locked=False, run=run) == ["iptables", "ip6tables"]
    assert ["iptables", "-F", "CANOPY-EGRESS"] in run.calls
    assert ["ip6tables", "-F", "CANOPY-EGRESS"] in run.calls
    assert run.appended() == [] and run.appended("ip6tables") == []
    # The jump already exists (-C succeeded): not inserted twice.
    assert not any(c[1] == "-I" for c in run.calls)


def test_a_locked_box_whose_rules_are_refused_does_not_come_up_open(cloud_runner, monkeypatch):
    monkeypatch.setattr(cloud_runner.shutil, "which", lambda t: "/sbin/ip6tables")
    with pytest.raises(RuntimeError, match="egress rules"):
        cloud_runner.egress_rules(UNITS, locked=True, run=Recorder(fail=lambda argv: argv[1] == "-A"))
    # ip6tables too: a v6 path left open is a way out.
    with pytest.raises(RuntimeError, match="egress rules"):
        cloud_runner.egress_rules(UNITS, locked=True,
                                  run=Recorder(fail=lambda argv: argv[0] == "ip6tables" and argv[1] == "-A"))


# ---- the plan: the box's floor, ZDR mode, and canopy-web ------------------------

def test_canopy_web_locks_one_unit_and_adds_its_hosts(cloud_runner):
    plan = cloud_runner.egress_plan(BOX, UNITS, {"cloud-zdr-1-agent-eva": {"locked": True,
                                                                          "allow": ["slack.com"]}})
    assert plan["dimagi"]["locked"] is False
    assert plan["agent-eva"]["locked"] is True and "slack.com" in plan["agent-eva"]["allow"]
    assert "slack.com" not in plan["dimagi"]["allow"]


def test_canopy_web_cannot_unlock_what_the_box_locks(cloud_runner):
    for box in ({**BOX, "RUNNER_EGRESS": "locked"}, {**BOX, "RUNNER_DATA_MODE": "zdr"}):
        plan = cloud_runner.egress_plan(box, UNITS, {"cloud-zdr-1": {"locked": False}})
        assert plan["dimagi"]["locked"] is True and plan["agent-eva"]["locked"] is True


def test_a_bad_host_from_canopy_web_is_skipped_not_fatal(cloud_runner):
    plan = cloud_runner.egress_plan(BOX, UNITS, {"cloud-zdr-1": {"locked": True,
                                                                "allow": ["ok.example.com", "bad/host"]}})
    assert "ok.example.com" in plan["dimagi"]["allow"] and "bad/host" not in plan["dimagi"]["allow"]


def test_the_box_reads_canopy_web_by_its_role(cloud_runner):
    seen = {}

    def post(url, body):
        seen.update(url=url, body=body)
        return 200, {"runners": [{"name": "cloud-zdr-1", "locked": True, "allow": ["slack.com"]}]}

    got = cloud_runner.fetch_box_egress("https://canopy.dimagi.com", post=post, proof=lambda url: "PROOF")
    assert seen["url"] == "https://canopy.dimagi.com/api/harness/runners/box-egress"
    assert seen["body"] == {"proof": "PROOF"}
    assert got == {"cloud-zdr-1": {"locked": True, "allow": ["slack.com"]}}
    assert cloud_runner.fetch_box_egress("https://x", post=lambda u, b: (401, {}), proof=lambda u: "P") is None
    assert cloud_runner.fetch_box_egress("https://x", post=post, proof=lambda u: "") is None


# ---- applying it --------------------------------------------------------------------

def _apply(cr, tmp_path, *, box=BOX, server=None, boot=False, idle=True, run=None):
    run = run or Recorder()
    applied = cr.apply_egress(box, UNITS, boot=boot, run=run, chown=lambda *a: None, etc=tmp_path,
                              systemd=tmp_path, fetch=lambda: server, idle=lambda u: idle)
    return applied, run


def test_boot_applies_the_plan_writes_env_and_starts_the_proxy(cloud_runner, monkeypatch, tmp_path):
    monkeypatch.setattr(cloud_runner.shutil, "which", lambda t: None)
    applied, run = _apply(cloud_runner, tmp_path, boot=True,
                          server={"cloud-zdr-1": {"locked": True, "allow": ["slack.com"]}})
    assert applied == {"dimagi": True, "agent-eva": False}
    assert "slack.com" in (tmp_path / "dimagi.egress.allow").read_text().split()
    env = (tmp_path / "dimagi.env").read_text()
    assert "HTTPS_PROXY=http://127.0.0.1:21001" in env and "RUNNER_EGRESS=locked" in env
    assert "HTTPS_PROXY" not in (tmp_path / "agent-eva.env").read_text()
    assert json.loads((tmp_path / "egress.json").read_text())["units"][0]["port"] == 21001
    assert "--egress-proxy" in (tmp_path / "canopy-egress.service").read_text()
    assert ["systemctl", "enable", "--now", "--no-block", "canopy-egress.service"] in run.calls
    assert {r[3] for r in run.appended()} == {"1001"}
    assert not any("canopy-runner@" in " ".join(c) for c in run.calls)   # nothing running yet


def test_a_lock_set_in_canopy_web_waits_for_an_idle_unit(cloud_runner, monkeypatch, tmp_path):
    monkeypatch.setattr(cloud_runner.shutil, "which", lambda t: None)
    _apply(cloud_runner, tmp_path, boot=True, server={})
    busy, run = _apply(cloud_runner, tmp_path, server={"cloud-zdr-1": {"locked": True}}, idle=False)
    assert busy["dimagi"] is False and run.appended() == []        # a turn is running: not yet
    done, run = _apply(cloud_runner, tmp_path, server={"cloud-zdr-1": {"locked": True}}, idle=True)
    assert done["dimagi"] is True
    assert "HTTPS_PROXY=http://127.0.0.1:21001" in (tmp_path / "dimagi.env").read_text()
    assert ["systemctl", "restart", "--no-block", "canopy-runner@dimagi.service"] in run.calls
    assert {r[3] for r in run.appended()} == {"1001"}


def test_an_unreadable_canopy_web_keeps_the_last_answer(cloud_runner, monkeypatch, tmp_path):
    monkeypatch.setattr(cloud_runner.shutil, "which", lambda t: None)
    _apply(cloud_runner, tmp_path, boot=True, server={"cloud-zdr-1": {"locked": True, "allow": ["slack.com"]}})
    applied, run = _apply(cloud_runner, tmp_path, server=None)      # read failed
    assert applied["dimagi"] is True
    assert "slack.com" in (tmp_path / "dimagi.egress.allow").read_text().split()


def test_nothing_locked_stops_the_proxy_and_opens_the_chain(cloud_runner, monkeypatch, tmp_path):
    monkeypatch.setattr(cloud_runner.shutil, "which", lambda t: None)
    applied, run = _apply(cloud_runner, tmp_path, boot=True, server={})
    assert applied == {"dimagi": False, "agent-eva": False}
    assert ["systemctl", "disable", "--now", "canopy-egress.service"] in run.calls
    assert ["iptables", "-F", "CANOPY-EGRESS"] in run.calls and run.appended() == []


def test_the_box_floor_locks_at_boot_with_no_canopy_web(cloud_runner, monkeypatch, tmp_path):
    monkeypatch.setattr(cloud_runner.shutil, "which", lambda t: None)
    applied, _run = _apply(cloud_runner, tmp_path, box={**BOX, "RUNNER_DATA_MODE": "zdr"}, boot=True,
                           server=None)
    assert applied == {"dimagi": True, "agent-eva": True}


def test_idle_reads_the_in_flight_marker(cloud_runner, tmp_path):
    unit = {**UNITS[0], "home": str(tmp_path)}
    assert cloud_runner.unit_idle(unit)                                   # no marker
    (tmp_path / ".canopy-runner").mkdir()
    marker = tmp_path / ".canopy-runner" / "in-flight"
    marker.write_text(json.dumps({"count": 1, "at": 1000.0}))
    assert not cloud_runner.unit_idle(unit, now=lambda: 1030.0)
    assert cloud_runner.unit_idle(unit, now=lambda: 1200.0)             # stale: not running
    marker.write_text(json.dumps({"count": 0, "at": 1000.0}))
    assert cloud_runner.unit_idle(unit, now=lambda: 1030.0)


def test_a_locked_units_env_carries_its_own_proxy(cloud_runner):
    unit = UNITS[0]
    env = cloud_runner.unit_env(unit, UNITS, BOX, uid=1001, locked=True)
    assert env["RUNNER_EGRESS"] == "locked" and env["CANOPY_EGRESS_PORT"] == "21001"
    for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        assert env[name] == "http://127.0.0.1:21001"
    assert "127.0.0.1" in env["NO_PROXY"]
    open_env = cloud_runner.unit_env(unit, UNITS, BOX, uid=1001, locked=False)
    assert "HTTPS_PROXY" not in open_env and open_env["RUNNER_EGRESS"] == ""
    # Not told: what the box alone says.
    assert cloud_runner.unit_env(unit, UNITS, {**BOX, "RUNNER_EGRESS": "locked"}, uid=1001)["RUNNER_EGRESS"]


def test_the_egress_settings_carry_over_on_an_in_place_move(cloud_runner):
    assert {"RUNNER_EGRESS", "RUNNER_EGRESS_ALLOW"} <= set(cloud_runner.BOX_ENV_KEYS)


# ---- the proxy ----------------------------------------------------------------------

async def _ask(port, request: bytes) -> bytes:
    r, w = await asyncio.open_connection("127.0.0.1", port)
    w.write(request)
    await w.drain()
    head = await asyncio.wait_for(r.readuntil(b"\r\n\r\n"), 5)
    return head, r, w


def _run_proxy(cr, allow_file: pathlib.Path, scenario):
    async def main():
        server = await asyncio.start_server(lambda r, w: cr._egress_handle(r, w, allow_file),
                                            "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            return await scenario(port)
        finally:
            server.close()
    return asyncio.run(main())


def test_the_proxy_refuses_a_host_off_the_list_and_anything_but_connect(cloud_runner, tmp_path):
    allow = tmp_path / "egress.allow"
    allow.write_text("api.anthropic.com\n")

    async def scenario(port):
        off, _r, _w = await _ask(port, b"CONNECT evil.example:443 HTTP/1.1\r\nHost: x\r\n\r\n")
        plain, _r, _w = await _ask(port, b"GET http://api.anthropic.com/ HTTP/1.1\r\n\r\n")
        port80, _r, _w = await _ask(port, b"CONNECT api.anthropic.com:80 HTTP/1.1\r\n\r\n")
        return off, plain, port80

    off, plain, port80 = _run_proxy(cloud_runner, allow, scenario)
    assert off.startswith(b"HTTP/1.1 403")
    assert plain.startswith(b"HTTP/1.1 405")
    assert port80.startswith(b"HTTP/1.1 403")


def test_the_proxy_refuses_an_allowed_name_that_resolves_inside(cloud_runner, tmp_path):
    """An allowed name pointed at IMDS, the VPC or loopback is not a way in."""
    allow = tmp_path / "egress.allow"
    allow.write_text("localhost\n")

    async def scenario(port):
        head, _r, _w = await _ask(port, b"CONNECT localhost:443 HTTP/1.1\r\n\r\n")
        return head

    assert _run_proxy(cloud_runner, allow, scenario).startswith(b"HTTP/1.1 403")


def test_the_proxy_tunnels_an_allowed_host(cloud_runner, tmp_path, monkeypatch):
    allow = tmp_path / "egress.allow"
    allow.write_text("localhost\n")
    monkeypatch.setattr(cloud_runner, "_egress_public", lambda addr: addr == "127.0.0.1")

    async def scenario(port):
        async def echo(r, w):
            w.write(b"hello " + await r.readline())
            await w.drain()
            w.close()
        upstream = await asyncio.start_server(echo, "127.0.0.1", 0)
        monkeypatch.setattr(cloud_runner, "EGRESS_UPSTREAM_PORT", upstream.sockets[0].getsockname()[1])
        target = f"CONNECT localhost:{cloud_runner.EGRESS_UPSTREAM_PORT} HTTP/1.1\r\n\r\n".encode()
        head, r, w = await _ask(port, target)
        w.write(b"world\n")
        await w.drain()
        body = await asyncio.wait_for(r.read(), 5)
        upstream.close()
        return head, body

    head, body = _run_proxy(cloud_runner, allow, scenario)
    assert head.startswith(b"HTTP/1.1 200")
    assert body == b"hello world\n"


def test_the_allowlist_is_read_per_connection(cloud_runner, tmp_path):
    allow = tmp_path / "egress.allow"
    allow.write_text("")

    async def scenario(port):
        first, _r, _w = await _ask(port, b"CONNECT localhost:443 HTTP/1.1\r\n\r\n")
        allow.unlink()                      # unreadable: refuses everything
        second, _r, _w = await _ask(port, b"CONNECT api.anthropic.com:443 HTTP/1.1\r\n\r\n")
        return first, second

    first, second = _run_proxy(cloud_runner, allow, scenario)
    assert first.startswith(b"HTTP/1.1 403") and second.startswith(b"HTTP/1.1 403")


# ---- the unit's own check, and ZDR mode -------------------------------------------

@pytest.fixture()
def locked(load_cloud_runner, monkeypatch):
    monkeypatch.setenv("RUNNER_EGRESS", "locked")
    return load_cloud_runner()


def _connect(proxy_up=True, direct_out=False):
    def connect(addr, timeout=None):
        if addr[0] == "127.0.0.1" and proxy_up:
            return type("S", (), {"close": lambda self: None})()
        if addr[0] != "127.0.0.1" and direct_out:
            return type("S", (), {"close": lambda self: None})()
        raise ConnectionRefusedError("refused")
    return connect


def test_the_probe_holds_when_the_proxy_answers_and_nothing_gets_out(locked):
    assert locked.egress_probe(connect=_connect())[0] == "ok"


def test_the_probe_fails_when_the_proxy_is_down(locked):
    status, detail = locked.egress_probe(connect=_connect(proxy_up=False))
    assert status == "fail" and "not answering" in detail


def test_the_probe_fails_when_a_direct_connection_gets_out(locked):
    status, detail = locked.egress_probe(connect=_connect(direct_out=True))
    assert status == "fail" and "rules are not in place" in detail


def test_the_probe_is_cached_a_minute(locked):
    t = [1000.0]
    assert locked.egress_probe(connect=_connect(), now=lambda: t[0])[0] == "ok"
    t[0] += 30
    assert locked.egress_probe(connect=_connect(direct_out=True), now=lambda: t[0])[0] == "ok"
    t[0] += 31
    assert locked.egress_probe(connect=_connect(direct_out=True), now=lambda: t[0])[0] == "fail"


def test_an_open_box_has_no_egress_check(cloud_runner):
    cloud_runner._egress_check()
    assert "egress" not in cloud_runner._HEALTH


def test_a_locked_box_reports_the_check(locked, monkeypatch):
    monkeypatch.setattr(locked, "egress_probe", lambda **k: ("ok", "locked: out only through the proxy"))
    locked._egress_check()
    assert locked._HEALTH["egress"]["status"] == "ok"


@pytest.fixture()
def zdr_box(load_cloud_runner, monkeypatch):
    def make(egress: str):
        monkeypatch.setenv("RUNNER_DATA_MODE", "zdr")
        monkeypatch.setenv("RUNNER_EGRESS", egress)
        cr = load_cloud_runner()
        cr._CLAUDE_CREDS.clear()
        cr._CLAUDE_CREDS.extend(cr._cascade_from({"claude_api_key": "sk-ant-zdr"}))
        return cr
    return make


def test_zdr_mode_with_egress_open_is_not_ready(zdr_box):
    cr = zdr_box("")
    ok, note = cr.zdr_readiness()
    assert not ok and "started open" in note
    cr._egress_check()
    assert cr._HEALTH["egress"]["status"] == "fail"


def test_zdr_mode_whose_lock_is_not_holding_is_not_ready(zdr_box, monkeypatch):
    cr = zdr_box("locked")
    monkeypatch.setattr(cr, "egress_probe", lambda **k: ("fail", "the egress proxy is not answering"))
    ok, note = cr.zdr_readiness()
    assert not ok and "not holding" in note
    monkeypatch.setattr(cr, "egress_probe", lambda **k: ("ok", "locked"))
    assert cr.zdr_readiness() == (True, "")


def test_the_posture_hash_covers_the_egress_setting(zdr_box):
    locked_hash = zdr_box("locked").zdr_posture()["data_mode_hash"]
    assert zdr_box("").zdr_posture()["data_mode_hash"] != locked_hash
