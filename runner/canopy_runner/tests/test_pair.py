"""`canopy-runner pair` against a fake control plane — never the real server.

The load-bearing property is idempotency: a box that is already paired must never
get a second runner, however many times the installer runs."""
import json
import os
import stat
from pathlib import Path

import pytest

from canopy_runner import pair
from canopy_runner.pair import PairError

WS_BOTH = [{"slug": "connect"}, {"slug": "dimagi"}]
MINE = [
    {"id": "jj-1", "name": "jj-mbp-cdp", "can_manage": True,
     "capabilities": {"agents": ["ace", "echo", "hal"]}},
    {"id": "ace-1", "name": "acedimagi-mbp-cdp", "can_manage": True,
     "capabilities": {"agents": ["ada", "echo"]}},
    {"id": "cloud-1", "name": "cloud-ec2-1", "can_manage": True, "capabilities": {}},
    {"id": "theirs", "name": "someone-else", "can_manage": False,
     "capabilities": {"agents": ["secret-agent"]}},
]


class FakeClient:
    """Records every call; answers from canned data. One instance per test, handed
    out by the factory so the test can inspect it afterwards."""

    def __init__(self, workspaces=None, runners=None, created_id="new-1"):
        self.workspaces = WS_BOTH if workspaces is None else workspaces
        self.runners = list(MINE if runners is None else runners)
        self.created_id = created_id
        self.calls: list[tuple] = []
        self.init_args: tuple = ()

    def factory(self, base_url, token):
        self.init_args = (base_url, token)
        return self

    def _call_api(self, path, *, method, body=None, label="", retry=True):
        self.calls.append((method, path, body, retry))
        assert (method, path) == ("GET", "/workspaces/")
        return 200, self.workspaces

    def _call(self, method, path, body=None, *, retry=True):
        self.calls.append((method, path, body, retry))
        if (method, path) == ("GET", "/runners/"):
            return 200, self.runners
        if (method, path) == ("POST", "/runners/"):
            return 201, {"id": self.created_id, **body}
        raise AssertionError(f"unexpected call {method} {path}")

    def posts(self):
        return [c for c in self.calls if c[0] == "POST"]


@pytest.fixture
def env(tmp_path):
    """A temp HOME with a PAT, plus a /Users root holding two sibling accounts."""
    home = tmp_path / "Users" / "newbie"
    (home / ".claude" / "canopy").mkdir(parents=True)
    tok = home / ".claude" / "canopy" / "workbench-token"
    tok.write_text("pat-123\n")
    support = home / "Library" / "Application Support" / "Emdash"
    support.mkdir(parents=True)
    (support / "emdash4.db").write_text("")
    for user, cdp, hook in (("jj", 9222, 8787), ("ace", 9223, 8788)):
        d = tmp_path / "Users" / user / ".canopy"
        d.mkdir(parents=True)
        (d / "runner.json").write_text(json.dumps({"cdp_port": cdp, "hook_port": hook}))
    launched = []

    def fake_launcher(port, h):
        launched.append((port, h))
        return "created"

    return {
        "home": home, "users_root": tmp_path / "Users", "token_ref": f"@{tok}",
        "config": home / ".canopy" / "runner.json", "launched": launched,
        "launcher": fake_launcher,
    }


def _run(env, client, **kw):
    kw.setdefault("workspace", "dimagi")
    kw.setdefault("token_ref", env["token_ref"])
    return pair.run_pair(
        env["config"], home=env["home"], users_root=env["users_root"],
        client_factory=client.factory,
        is_free=lambda p: True, ensure_launcher_fn=env["launcher"],
        out=lambda *_: None, **kw)


# --- fresh pairing --------------------------------------------------------------


def test_fresh_account_pairs_once_and_writes_config(env, monkeypatch):
    monkeypatch.setattr(pair, "macos_user", lambda: "newbie")
    fake = FakeClient()
    assert _run(env, fake) == 0

    [post] = fake.posts()
    assert post == ("POST", "/runners/", {
        "name": "newbie-mbp-cdp", "kind": "emdash", "workspace": "dimagi",
        "capabilities": {"agents": ["ace", "ada", "echo", "hal"], "sessions": True},
    }, False)  # never retried: a re-sent POST would pair a duplicate
    # sessions: a laptop runner takes Slack/chat turns; without it every chat turn
    # for its agents sits UNROUTED (2026-10-02, a second ACE laptop).
    assert fake.init_args == (pair.DEFAULT_BASE_URL, "pat-123")

    cfg = json.loads(env["config"].read_text())
    assert cfg["runner_id"] == "new-1"
    assert cfg["token"] == env["token_ref"]  # a reference, never the PAT itself
    assert (cfg["cdp_port"], cfg["hook_port"]) == (9224, 8789)  # past both siblings
    assert cfg["emdash_db"].endswith("Application Support/Emdash/emdash4.db")
    assert cfg["forward_sessions"] is True and cfg["mailboxes"] == {}
    # Readable by the NEXT account's sibling scan; holds no secret.
    assert stat.S_IMODE(os.stat(env["config"]).st_mode) == 0o644
    assert env["launched"] == [(9224, env["home"])]


def test_config_loads_as_a_runner_config(env, monkeypatch):
    from canopy_runner.config import Config

    monkeypatch.setattr(pair, "macos_user", lambda: "newbie")
    _run(env, FakeClient())
    cfg = Config.load(env["config"])
    assert cfg.token == "pat-123" and cfg.cdp_port == 9224 and cfg.hook_port == 8789


def test_overrides_are_honoured(env):
    fake = FakeClient()
    _run(env, fake, name="custom", agents=["eva"], cdp_port=9300, hook_port=8800)
    [post] = fake.posts()
    assert post[2]["name"] == "custom" and post[2]["capabilities"] == {"agents": ["eva"], "sessions": True}
    cfg = json.loads(env["config"].read_text())
    assert (cfg["cdp_port"], cfg["hook_port"]) == (9300, 8800)


def test_single_workspace_is_used_without_the_flag(env):
    fake = FakeClient(workspaces=[{"slug": "dimagi"}])
    _run(env, fake, workspace="")
    assert fake.posts()[0][2]["workspace"] == "dimagi"


def test_several_workspaces_refuse_to_guess(env):
    fake = FakeClient()
    with pytest.raises(PairError, match=r"2 workspaces \(connect, dimagi\).*--workspace"):
        _run(env, fake, workspace="")
    assert not fake.posts() and not env["config"].exists()


def test_name_clash_refuses_and_points_at_adopt(env):
    fake = FakeClient()
    with pytest.raises(PairError, match="--runner-id jj-1"):
        _run(env, fake, name="jj-mbp-cdp")
    assert not fake.posts() and not env["config"].exists()


def test_adopting_an_existing_runner_writes_config_without_pairing(env):
    fake = FakeClient()
    _run(env, fake, runner_id="ace-1")
    assert not fake.posts()
    assert json.loads(env["config"].read_text())["runner_id"] == "ace-1"


def test_cannot_adopt_someone_elses_runner(env):
    with pytest.raises(PairError, match="not one you manage"):
        _run(env, FakeClient(), runner_id="theirs")


def test_dry_run_neither_pairs_nor_writes(env):
    fake = FakeClient()
    assert _run(env, fake, dry_run=True) == 0
    assert not fake.posts() and not env["config"].exists() and not env["launched"]


def test_missing_pat_says_how_to_get_one(env, tmp_path):
    with pytest.raises(PairError, match="pat-mint"):
        _run(env, FakeClient(), token_ref=f"@{tmp_path / 'nope'}")


# --- idempotency: an already-paired box -------------------------------------------


def _existing_config(env, runner_id):
    env["config"].parent.mkdir(parents=True)
    env["config"].write_text(json.dumps({
        "base_url": "https://example.test/canopy", "token": env["token_ref"],
        "runner_id": runner_id, "cdp_port": 9230,
    }))
    return env["config"].read_bytes(), env["config"].stat().st_mtime_ns


def test_already_paired_box_is_left_alone(env):
    before, mtime = _existing_config(env, "ace-1")
    fake = FakeClient()
    assert _run(env, fake, workspace="") == 0
    assert not fake.posts()
    assert fake.init_args == ("https://example.test/canopy", "pat-123")  # the config's own
    assert env["config"].read_bytes() == before
    assert env["config"].stat().st_mtime_ns == mtime
    assert env["launched"] == [(9230, env["home"])]  # launcher kept in step with the config


def test_config_naming_an_unknown_runner_refuses_rather_than_pairing_again(env):
    before, _ = _existing_config(env, "retired-or-foreign")
    fake = FakeClient()
    with pytest.raises(PairError, match="unretire"):
        _run(env, fake)
    assert not fake.posts() and env["config"].read_bytes() == before


def test_config_without_runner_id_refuses(env):
    env["config"].parent.mkdir(parents=True)
    env["config"].write_text("{}")
    with pytest.raises(PairError, match="no runner_id"):
        _run(env, FakeClient())


# --- the pieces ---------------------------------------------------------------------


def test_default_agents_is_the_union_of_my_other_runners():
    assert pair.default_agents(MINE) == ["ace", "ada", "echo", "hal"]
    assert pair.default_agents(MINE, exclude_id="jj-1") == ["ada", "echo"]


def test_choose_port_skips_taken_and_listening():
    assert pair.choose_port(9222, {9222, 9223}, is_free=lambda p: p != 9224) == 9225


def test_sibling_ports_skip_own_home_and_junk(tmp_path):
    root = tmp_path / "Users"
    for user, body in (("a", {"cdp_port": 9222, "hook_port": 8787}),
                       ("me", {"cdp_port": 9999, "hook_port": 9998}),
                       ("junk", None)):
        d = root / user / ".canopy"
        d.mkdir(parents=True)
        (d / "runner.json").write_text("not json" if body is None else json.dumps(body))
    assert pair.sibling_ports(root, root / "me") == {9222, 8787}


@pytest.mark.parametrize("dirname", ["Emdash", "emdash"])
def test_find_emdash_db_keeps_the_real_case(tmp_path, dirname):
    d = tmp_path / "Library" / "Application Support" / dirname
    d.mkdir(parents=True)
    (d / "emdash4.db").write_text("")
    assert pair.find_emdash_db(tmp_path).parent.name == dirname


def test_find_emdash_db_defaults_when_emdash_never_ran(tmp_path):
    assert pair.find_emdash_db(tmp_path) == (
        tmp_path / "Library" / "Application Support" / "emdash" / "emdash4.db")


def test_port_free_sees_a_listener():
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen()
        assert not pair.port_free(s.getsockname()[1])


# --- launcher --------------------------------------------------------------------------


class FakeRun:
    def __init__(self, decompiled=""):
        self.decompiled = decompiled
        self.cmds = []

    def __call__(self, cmd, **kw):
        self.cmds.append(cmd)

        class R:
            returncode = 0
            stdout = self.decompiled
            stderr = ""
        if cmd[0] == "osacompile":
            (Path(cmd[2]) / "Contents").mkdir(parents=True)
        return R()


def test_launcher_is_built_for_the_port(tmp_path, monkeypatch):
    monkeypatch.setattr(pair.shutil, "which", lambda _: "/usr/bin/osacompile")
    run = FakeRun()
    status = pair.ensure_launcher(9224, tmp_path, run=run, system="Darwin")
    assert status.startswith("created")
    [cmd] = run.cmds
    assert cmd[:3] == ["osacompile", "-o", str(tmp_path / "Applications" / "Emdash CDP.app")]
    script = "\n".join(cmd[4::2])
    assert "if it is running then quit" in script
    assert "--remote-debugging-port=9224" in script


def test_launcher_left_alone_when_already_on_the_port(tmp_path, monkeypatch):
    monkeypatch.setattr(pair.shutil, "which", lambda _: "/usr/bin/osacompile")
    (tmp_path / "Applications" / "Emdash CDP.app").mkdir(parents=True)
    run = FakeRun(decompiled="... --remote-debugging-port=9224\"")
    assert pair.ensure_launcher(9224, tmp_path, run=run, system="Darwin").startswith("unchanged")
    assert [c[0] for c in run.cmds] == ["osadecompile"]


def test_launcher_rebuilt_when_on_another_port(tmp_path, monkeypatch):
    monkeypatch.setattr(pair.shutil, "which", lambda _: "/usr/bin/osacompile")
    (tmp_path / "Applications" / "Emdash CDP.app").mkdir(parents=True)
    run = FakeRun(decompiled="--remote-debugging-port=9222")
    assert pair.ensure_launcher(9224, tmp_path, run=run, system="Darwin").startswith("updated")
    assert [c[0] for c in run.cmds] == ["osadecompile", "osacompile"]


def test_launcher_skipped_off_macos(tmp_path):
    assert pair.ensure_launcher(9224, tmp_path, system="Linux").startswith("skipped")


# --- re-pairing with emdash already running (2026-10-02) ----------------------


def _devtools(env, port):
    f = env["home"] / "Library" / "Application Support" / "Emdash" / "DevToolsActivePort"
    f.write_text(f"{port}\n/devtools/browser/abc\n")


def test_running_emdash_port_is_used_rather_than_the_next_free_one(env, monkeypatch):
    """emdash already listens on 9225 on this account: pair must drive THAT port.
    The picker alone would choose a port nothing listens on (cdp_down forever)."""
    monkeypatch.setattr(pair, "macos_user", lambda: "newbie")
    _devtools(env, 9225)
    lines = []
    pair.run_pair(
        env["config"], workspace="dimagi", token_ref=env["token_ref"], home=env["home"],
        users_root=env["users_root"], client_factory=FakeClient().factory,
        is_free=lambda p: p != 9225,  # it is in use — by emdash itself
        live_cdp_port=lambda h: pair.emdash_live_cdp_port(h, is_up=lambda p: True),
        ensure_launcher_fn=env["launcher"], out=lines.append)
    assert json.loads(env["config"].read_text())["cdp_port"] == 9225
    assert any("DevToolsActivePort" in line for line in lines)


def test_stale_devtools_file_is_ignored(env):
    """emdash quit, the file stayed: nothing answers, so fall back to the picker."""
    _devtools(env, 9225)
    assert pair.emdash_live_cdp_port(env["home"], is_up=lambda p: False) is None


def test_live_port_claimed_by_a_sibling_falls_back_to_the_picker(env, monkeypatch):
    monkeypatch.setattr(pair, "macos_user", lambda: "newbie")
    _devtools(env, 9223)  # the `ace` sibling's runner.json claims 9223
    _run(env, FakeClient(),
         live_cdp_port=lambda h: pair.emdash_live_cdp_port(h, is_up=lambda p: True))
    assert json.loads(env["config"].read_text())["cdp_port"] == 9224


def test_name_clash_with_someone_elses_runner_says_new_name_or_retire(env):
    fake = FakeClient()
    with pytest.raises(PairError, match="retire") as exc:
        _run(env, fake, name="someone-else")
    assert "--runner-id never transfers ownership" in str(exc.value)
    assert "--runner-id theirs" not in str(exc.value)  # no adopt hint that can't work
    assert not fake.posts()
