"""The machine half of several runners on one machine (spec
2026-10-19-runner-isolation-runners §1, §2, §5; plan Tasks 6.2 and 6.5): `Runners`
parsing and naming, each runner's environment, root's setup (users, files, the IMDS
rule), the ONE pair-by-role payload for every runner, and how the live single-runner
box is adopted (its runner name and KMS key kept).
"""
from __future__ import annotations

import json
import pathlib
import subprocess
from types import SimpleNamespace

import pytest

BOX = {"RUNNER_NAME": "cloud-ec2-9", "RUNNER_WORKSPACE": "dimagi", "CANOPY_BASE_URL": "https://canopy.dimagi.com",
       "RUNNER_AGENTS": "ace,ada,eva", "AGENT_SLUGS": "ace,ada,eva", "RUNNER_PROJECTS": "canopy-web",
       "RUNNER_SESSIONS": "1", "RUNNER_EXECUTOR": "acp",
       "RUNNERS": "tree:dimagi,tree:marshellis,agent:eva"}


# ---- Runners: parsing and naming ---------------------------------------------

def test_no_runners_means_one_tree_runner_named_as_the_box(cloud_runner):
    [runner] = cloud_runner.parse_runners("", workspace="dimagi", box="cloud-ec2-2")
    assert runner["id"] == "dimagi" and runner["kind"] == "tree" and runner["workspace"] == "dimagi"
    # The box's own name: the live box keeps its runner when it moves to several runners.
    assert runner["name"] == "cloud-ec2-2" and runner["user"] == "cu-dimagi"


def test_runners_are_named_box_dash_runner(cloud_runner):
    runners = cloud_runner.parse_runners("tree:dimagi,tree:marshellis,agent:eva", workspace="dimagi",
                                     box="cloud-ec2-2")
    assert [(u["id"], u["kind"], u["workspace"], u["slug"], u["name"], u["user"]) for u in runners] == [
        ("dimagi", "tree", "dimagi", "", "cloud-ec2-2", "cu-dimagi"),
        ("marshellis", "tree", "marshellis", "", "cloud-ec2-2-marshellis", "cu-marshellis"),
        ("agent-eva", "agent", "dimagi", "eva", "cloud-ec2-2-agent-eva", "cu-agent-eva"),
    ]
    [u] = cloud_runner.parse_runners("agent:fam@marshellis", workspace="dimagi", box="b")
    assert u["workspace"] == "marshellis" and u["name"] == "b-agent-fam"


@pytest.mark.parametrize("spec", ["tree:dimagi,tree:dimagi", "agent:eva,agent:eva", "cell:x", "tree:",
                                  "tree:Bad_Name", "agent:" + "x" * 40])
def test_a_spec_that_is_not_runners_is_refused(cloud_runner, spec):
    with pytest.raises(ValueError):
        cloud_runner.parse_runners(spec, workspace="dimagi", box="cloud-ec2-2")


def test_the_box_runner_keeps_the_boxs_original_kms_key(cloud_runner):
    runners = cloud_runner.parse_runners("tree:dimagi,agent:eva", workspace="dimagi", box="cloud-ec2-2")
    role = "canopy-cloud-runner-2-RunnerRole-ABC"
    # The alias the `-ecdh` stack has always given the box: every wrap stays valid.
    assert cloud_runner.runner_kms_key(runners[0], "cloud-ec2-2", role) == f"alias/canopy-runner-ecdh/{role}"
    assert cloud_runner.runner_kms_key(runners[1], "cloud-ec2-2", role) == \
        f"alias/canopy-runner-ecdh/{role}/agent-eva"


# ---- each runner's environment ----------------------------------------------------

def test_a_runner_env_holds_no_pat_no_claude_token_no_box_keyring_password(cloud_runner):
    runners = cloud_runner.parse_runners(BOX["RUNNERS"], workspace="dimagi", box="cloud-ec2-9")
    for runner in runners:
        env = cloud_runner.runner_env(runner, runners, BOX, uid=2001)
        for banned in ("CANOPY_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN", "GOG_KEYRING_PASSWORD", "ANTHROPIC_API_KEY"):
            assert banned not in env
        assert env["CANOPY_RUNNER_INSTANCE"] == runner["id"] and env["RUNNER_NAME"] == runner["name"]
        assert env["HOME"] == runner["home"] and env["WORK_DIR"].startswith(runner["home"])
        assert env["STATE_FILE"].startswith(runner["home"]) and env["IN_FLIGHT_FILE"].startswith(runner["home"])
        assert env["CANOPY_DEVICE_KEY_KIND"] == "software"
        assert env["CANOPY_ECDH_BROKER"] == "/run/canopy-kms/sock"
        assert env["CANOPY_TURN_ENV_ROOT"] == "/run/canopy/2001"
        assert env["CANOPY_GOG_SECRET_FILE"].endswith(f"/{runner['id']}.gog-secret")


def test_agents_follow_their_runners(cloud_runner):
    runners = cloud_runner.parse_runners(BOX["RUNNERS"], workspace="dimagi", box="cloud-ec2-9")
    by = {u["id"]: cloud_runner.runner_env(u, runners, BOX, uid=1) for u in runners}
    # eva has a runner of its own: the box's tree runner no longer carries it.
    assert by["dimagi"]["RUNNER_AGENTS"] == "ace,ada" and by["dimagi"]["AGENT_SLUGS"] == "ace,ada"
    assert by["dimagi"]["RUNNER_PROJECTS"] == "canopy-web"
    assert by["agent-eva"]["RUNNER_AGENTS"] == "eva" and by["agent-eva"]["RUNNER_PROJECTS"] == ""
    assert by["agent-eva"]["CANOPY_SERVES_KIND"] == "agent" and by["agent-eva"]["CANOPY_SERVES_AGENT"] == "eva"
    assert by["marshellis"]["RUNNER_AGENTS"] == "" and by["marshellis"]["RUNNER_WORKSPACE"] == "marshellis"


def test_an_agent_runner_declares_what_it_serves(load_cloud_runner, monkeypatch):
    monkeypatch.setenv("CANOPY_RUNNER_INSTANCE", "agent-eva")
    monkeypatch.setenv("CANOPY_SERVES_KIND", "agent")
    monkeypatch.setenv("CANOPY_SERVES_AGENT", "eva")
    cr = load_cloud_runner()
    assert cr.RUNNER_CAPS["serves"] == {"kind": "agent", "slug": "eva"}
    # The same under the old key, for a canopy-web from before the rename (remove in the follow-up).
    assert cr.RUNNER_CAPS["unit"] == cr.RUNNER_CAPS["serves"]


def test_a_runner_still_reads_the_environment_it_was_given_before_the_rename(load_cloud_runner, monkeypatch):
    """Its env file says CANOPY_UNIT* until update_runner.sh moves it (remove in the follow-up)."""
    for new in ("CANOPY_RUNNER_INSTANCE", "CANOPY_SERVES_KIND", "CANOPY_SERVES_AGENT"):
        monkeypatch.delenv(new, raising=False)
    monkeypatch.setenv("CANOPY_UNIT", "agent-eva")
    monkeypatch.setenv("CANOPY_UNIT_KIND", "agent")
    monkeypatch.setenv("CANOPY_UNIT_AGENT", "eva")
    cr = load_cloud_runner()
    assert cr.RUNNER_INSTANCE == "agent-eva"
    assert cr.RUNNER_CAPS["serves"] == {"kind": "agent", "slug": "eva"}


# ---- root's setup -----------------------------------------------------------------

@pytest.fixture
def host(cloud_runner, monkeypatch, tmp_path):
    """A fake box: users made by a fake useradd, files under tmp_path, chown recorded."""
    monkeypatch.setattr(cloud_runner, "RUNNER_HOME_ROOT", str(tmp_path / "home"))
    monkeypatch.setattr(cloud_runner, "TURN_ENV_BASE", tmp_path / "run" / "canopy")
    users: dict = {}
    calls: list = []
    owners: dict = {}

    def run(argv, **kw):
        calls.append(list(argv))
        if argv[:2] == ["id", "-u"]:
            return subprocess.CompletedProcess(argv, 0 if argv[2] in users else 1, "", "")
        if argv[0] == "useradd":
            users[argv[-1]] = 2000 + len(users) + 1
        return subprocess.CompletedProcess(argv, 0, "", "")

    def uid_of(user):
        return SimpleNamespace(pw_uid=users[user], pw_gid=users[user])

    def chown(path, uid, gid):
        owners[path.removesuffix(".tmp")] = uid

    etc = tmp_path / "etc"
    return SimpleNamespace(run=run, uid_of=uid_of, chown=chown, etc=etc, users=users, calls=calls,
                           owners=owners, root=tmp_path)


def _setup(cloud_runner, host, box=BOX):
    # parse_runners reads RUNNER_HOME_ROOT when it builds each runner's home.
    return cloud_runner.setup_runners(box, run=host.run, chown=host.chown, uid_of=host.uid_of, etc=host.etc,
                                    role_name="RunnerRole-ABC")


def test_setup_makes_a_user_home_env_and_secret_per_runner(cloud_runner, host):
    runners = _setup(cloud_runner, host)
    assert sorted(host.users) == ["cu-agent-eva", "cu-dimagi", "cu-marshellis"]
    for runner in runners:
        home = pathlib.Path(runner["home"])
        assert (home.stat().st_mode & 0o777) == 0o700 and host.owners[str(home)] == runner["uid"]
        env_file = host.etc / f"{runner['id']}.env"
        assert (env_file.stat().st_mode & 0o777) == 0o600 and host.owners[str(env_file)] == runner["uid"]
        env = cloud_runner.read_env_file(env_file)
        assert "CANOPY_TOKEN" not in env and env["CANOPY_RUNNER_INSTANCE"] == runner["id"]
        secret = host.etc / f"{runner['id']}.gog-secret"
        assert len(secret.read_bytes()) == 32 and (secret.stat().st_mode & 0o777) == 0o400
        root = pathlib.Path(env["CANOPY_TURN_ENV_ROOT"])
        assert root.is_dir() and (root.stat().st_mode & 0o777) == 0o700 and host.owners[str(root)] == runner["uid"]
    doc = json.loads((host.etc / "runners.json").read_text())
    assert [(u["id"], u["uid"], u["kms_key"]) for u in doc["runners"]] == [
        ("dimagi", runners[0]["uid"], "alias/canopy-runner-ecdh/RunnerRole-ABC"),
        ("marshellis", runners[1]["uid"], "alias/canopy-runner-ecdh/RunnerRole-ABC/marshellis"),
        ("agent-eva", runners[2]["uid"], "alias/canopy-runner-ecdh/RunnerRole-ABC/agent-eva"),
    ]
    assert (host.etc / "runners.json").stat().st_mode & 0o777 == 0o644


def test_setup_is_idempotent_and_never_rotates_a_gog_secret(cloud_runner, host):
    _setup(cloud_runner, host)
    before = (host.etc / "dimagi.gog-secret").read_bytes()
    useradds = sum(1 for c in host.calls if c[0] == "useradd")
    _setup(cloud_runner, host)
    assert (host.etc / "dimagi.gog-secret").read_bytes() == before
    assert sum(1 for c in host.calls if c[0] == "useradd") == useradds


def test_the_imds_rule_rejects_every_uid_but_roots(cloud_runner, monkeypatch):
    calls = []

    def run(argv, **kw):
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 1 if argv[1] == "-C" else 0, "", "")

    monkeypatch.setattr(cloud_runner.shutil, "which", lambda tool: f"/usr/sbin/{tool}")
    assert cloud_runner.imds_rules(run=run) == ["iptables", "ip6tables"]
    inserts = [c for c in calls if c[1] == "-I"]
    assert inserts[0] == ["iptables", "-I", "OUTPUT", "1", "-d", "169.254.169.254", "-m", "owner", "!",
                          "--uid-owner", "0", "-j", "REJECT"]
    assert inserts[1][5] == "fd00:ec2::254"
    # Already there: nothing inserted twice.
    calls.clear()
    assert cloud_runner.imds_rules(run=lambda argv, **kw: subprocess.CompletedProcess(argv, 0, "", "")) == []


def test_without_the_imds_rule_setup_fails_loudly(cloud_runner, monkeypatch):
    monkeypatch.setattr(cloud_runner.shutil, "which", lambda tool: None)
    with pytest.raises(RuntimeError):
        cloud_runner.imds_rules(run=lambda argv, **kw: subprocess.CompletedProcess(argv, 1, "", "denied"))


# ---- canopy-pair: every runner in one call ------------------------------------------

def _jwk(n):
    return {"kty": "EC", "crv": "P-256", "x": f"x{n}", "y": f"y{n}"}


def test_the_pair_runners_payload_is_one_proof_and_every_runner(cloud_runner):
    runners = cloud_runner.parse_runners(BOX["RUNNERS"], workspace="dimagi", box="cloud-ec2-9")
    keys = {u["id"]: {"public_jwk": _jwk(i), "jkt": f"j{i}", "ecdh_public_jwk": _jwk(10 + i)}
            for i, u in enumerate(runners)}
    keys["marshellis"].pop("ecdh_public_jwk")   # its KMS key does not exist yet
    envs = {u["id"]: {"RUNNER_SESSIONS": "1"} for u in runners}
    body = cloud_runner.pair_runners_payload("cloud-ec2-9", runners, keys, "PROOF", runner_envs=envs)
    assert body["name"] == "cloud-ec2-9" and body["proof"] == "PROOF" and body["kind"] == "cloud"
    assert [u["name"] for u in body["runners"]] == ["cloud-ec2-9", "cloud-ec2-9-marshellis",
                                                   "cloud-ec2-9-agent-eva"]
    first, second, third = body["runners"]
    assert first["public_jwk"] == _jwk(0) and first["ecdh_store"] == "kms-broker"
    assert "ecdh_public_jwk" not in second
    assert third["capabilities"] == {"sessions": True, "serves": {"kind": "agent", "slug": "eva"},
                                     "unit": {"kind": "agent", "slug": "eva"}}
    # The list again under the old key, for a canopy-web from before the rename (remove in the follow-up).
    assert body["units"] == body["runners"]


@pytest.fixture
def paired_box(cloud_runner, host, monkeypatch):
    runners = _setup(cloud_runner, host)
    monkeypatch.setattr(cloud_runner, "RUNNER_ETC", host.etc)
    monkeypatch.setattr(cloud_runner, "RUNNERS_FILE", host.etc / "runners.json")
    monkeypatch.setattr(cloud_runner, "_aws_instance_credentials", lambda: {"access_key": "A"})
    monkeypatch.setattr(cloud_runner, "aws_proof", lambda url, creds=None: "PROOF")
    monkeypatch.setattr(cloud_runner, "_log", lambda m: None)
    cli: list = []

    def run(argv, **kw):
        cli.append(argv)
        cmd = argv[argv.index("-i") + 1:]
        cmd = [c for c in cmd if "=" not in c or c.startswith("--")]
        user = argv[2]
        n = user.removeprefix("cu-")
        if cmd[:2] == ["bash", "-c"]:
            return subprocess.CompletedProcess(argv, 0, "/home/x/.local/bin/canopy\n", "")
        if "--public-jwk" in cmd:
            return subprocess.CompletedProcess(argv, 0, json.dumps(
                {"kind": "software", "jkt": f"jkt-{n}", "jwk": _jwk(n), "created": True}), "")
        if "--ecdh-jwk" in cmd:
            return subprocess.CompletedProcess(argv, 0, json.dumps(
                {"kind": "kms-broker", "jkt": f"e-{n}", "jwk": _jwk("e" + n)}), "")
        return subprocess.CompletedProcess(argv, 0, "{}", "")

    return SimpleNamespace(runners=runners, run=run, cli=cli, host=host)


def test_canopy_pair_pairs_every_runner_and_writes_each_state(cloud_runner, paired_box):
    sent = []
    reply = {"status": "paired", "runners": {u["name"]: f"rid-{u['id']}" for u in paired_box.runners},
             "device_keys": {u["name"]: f"key-{u['id']}" for u in paired_box.runners}}

    def post(url, body):
        sent.append(body)
        return (202, {"status": "waiting"}) if len(sent) == 1 else (201, reply)

    assert cloud_runner.pair_runners_main(run=paired_box.run, post=post, sleep=lambda s: None,
                                        chown=paired_box.host.chown) == 0
    assert len(sent) == 2 and len(sent[1]["runners"]) == 3   # one call per attempt, every runner in it
    for runner in paired_box.runners:
        state = json.loads((pathlib.Path(runner["home"]) / ".canopy-cloud-runner.json").read_text())
        assert state == {"runner_id": f"rid-{runner['id']}", "paired_by": "invitation", "instance": runner["id"],
                         "jkt": f"jkt-{runner['id']}"}
        assert paired_box.host.owners[str(pathlib.Path(runner["home"]) / ".canopy-cloud-runner.json")] == runner["uid"]
    registered = [c for c in paired_box.cli if "--registered" in c]
    assert len(registered) == 3
    assert all(c[:3] == ["runuser", "-u", c[2]] and c[2].startswith("cu-") for c in registered)
    # Every CLI call ran AS the runner's user, never as root.
    assert all(c[0] == "runuser" for c in paired_box.cli)


def test_canopy_pair_leaves_runners_paired_with_the_keys_they_hold(cloud_runner, paired_box):
    for runner in paired_box.runners:
        (pathlib.Path(runner["home"]) / ".canopy-cloud-runner.json").write_text(json.dumps(
            {"runner_id": "rid", "jkt": f"jkt-{runner['id']}"}))
    assert cloud_runner.pair_runners_main(run=paired_box.run, post=lambda u, b: pytest.fail("posted"),
                                        sleep=lambda s: None) == 0


def test_a_replaced_instance_pairs_again_with_its_new_keys(cloud_runner, paired_box):
    for runner in paired_box.runners:   # an old instance's state: other keys
        (pathlib.Path(runner["home"]) / ".canopy-cloud-runner.json").write_text(json.dumps(
            {"runner_id": "rid", "jkt": "old"}))
    sent = []
    reply = {"runners": {u["name"]: "rid" for u in paired_box.runners}}
    assert cloud_runner.pair_runners_main(run=paired_box.run, post=lambda u, b: (sent.append(b), (200, reply))[1],
                                        sleep=lambda s: None, chown=paired_box.host.chown) == 0
    assert len(sent) == 1


# ---- the names from before the rename (remove in the follow-up) -----------------------

def test_a_canopy_web_from_before_the_rename_answers_under_units(cloud_runner, paired_box):
    reply = {"status": "paired", "units": {u["name"]: f"rid-{u['id']}" for u in paired_box.runners}}
    assert cloud_runner.pair_runners_main(run=paired_box.run, post=lambda u, b: (201, reply),
                                          sleep=lambda s: None, chown=paired_box.host.chown) == 0
    for runner in paired_box.runners:
        state = json.loads((pathlib.Path(runner["home"]) / ".canopy-cloud-runner.json").read_text())
        assert state["runner_id"] == f"rid-{runner['id']}"


def test_a_box_not_yet_moved_reads_units_json_and_runner_units(cloud_runner, host, monkeypatch):
    """Until update_runner.sh moves it, a box has units.json (its list under `units`) and
    box.env's RUNNER_UNITS — the runner reads both."""
    runners = _setup(cloud_runner, host, {**{k: v for k, v in BOX.items() if k != "RUNNERS"},
                                          "RUNNER_UNITS": "tree:dimagi,agent:eva"})
    assert [r["id"] for r in runners] == ["dimagi", "agent-eva"]
    doc = json.loads((host.etc / "runners.json").read_text())
    (host.etc / "runners.json").unlink()
    (host.etc / "units.json").write_text(json.dumps({**doc, "units": doc.pop("runners")}))
    monkeypatch.setattr(cloud_runner, "RUNNERS_FILE", host.etc / "runners.json")
    monkeypatch.setattr(cloud_runner, "OLD_RUNNERS_FILE", host.etc / "units.json")
    assert [r["id"] for r in cloud_runner.runners_of(cloud_runner.load_runners())] == ["dimagi", "agent-eva"]
    assert cloud_runner.env_or_old("RUNNERS", "", {"RUNNER_UNITS": "tree:x"}) == "tree:x"
    assert cloud_runner.env_or_old("RUNNERS", "", {"RUNNERS": "tree:y", "RUNNER_UNITS": "tree:x"}) == "tree:y"


def test_root_still_answers_to_the_flags_its_systemd_units_were_given(cloud_runner):
    """A box's installed canopy-units.service / canopy-pair.service call `--setup-units` /
    `--pair-units` until update_runner.sh rewrites them."""
    assert cloud_runner.OLD_FLAGS == {"--install-units": "--install-runners", "--setup-units": "--setup-runners",
                                      "--pair-units": "--pair-runners"}
    src = pathlib.Path(cloud_runner.__file__).read_text()
    assert "_step = OLD_FLAGS.get(sys.argv[1], sys.argv[1])" in src


# ---- adopting the live box (Review Focus 1) ------------------------------------------

def test_the_live_box_adopts_as_its_default_runner(cloud_runner, host, monkeypatch):
    """cloud-ec2-2 today: no Runners. Its one runner is `tree:<workspace>`, named
    cloud-ec2-2 (so canopy-web re-attaches its runner by machine and name, keeping the
    id), and its ECDH key is the stack's existing KMS key (so the broker hands out the
    same public key: same ecdh jkt, every wrap kept). The server half is
    tests/test_machine_pairing.py::test_the_live_single_runner_box_is_adopted_…"""
    box = {**BOX, "RUNNER_NAME": "cloud-ec2-2", "RUNNERS": ""}
    [runner] = _setup(cloud_runner, host, box)
    doc = json.loads((host.etc / "runners.json").read_text())
    assert runner["name"] == "cloud-ec2-2"
    assert doc["runners"][0]["kms_key"] == "alias/canopy-runner-ecdh/RunnerRole-ABC"
    env = cloud_runner.read_env_file(host.etc / "dimagi.env")
    # The single runner still runs the whole box's agents and repos.
    assert env["RUNNER_AGENTS"] == "ace,ada,eva" and env["RUNNER_PROJECTS"] == "canopy-web"
    keys = {"dimagi": {"public_jwk": _jwk("new"), "jkt": "j", "ecdh_public_jwk": _jwk("kms")}}
    body = cloud_runner.pair_runners_payload("cloud-ec2-2", [runner], keys, "P", runner_envs={"dimagi": env})
    assert body["name"] == "cloud-ec2-2" and body["runners"][0]["name"] == "cloud-ec2-2"
    assert body["runners"][0]["ecdh_public_jwk"] == _jwk("kms")


def test_a_box_without_runners_runs_exactly_as_before(cloud_runner):
    """No CANOPY_RUNNER_INSTANCE: the legacy single service (canopy-runner.service) — aws-iam
    device proofs, its own pairing, its PAT fallback until it paired by invitation."""
    assert cloud_runner.RUNNER_INSTANCE == ""
    assert "serves" not in cloud_runner.RUNNER_CAPS
    assert cloud_runner._no_pat_fallback() is False


# ---- a runner on a shared machine never pairs or signs as the box ----------------------

def test_a_runner_has_no_pat_fallback(load_cloud_runner, monkeypatch, tmp_path):
    monkeypatch.setenv("CANOPY_RUNNER_INSTANCE", "dimagi")
    monkeypatch.setenv("CANOPY_TOKEN", "a-persons-pat")
    monkeypatch.setenv("STATE_FILE", str(tmp_path / "state.json"))
    cr = load_cloud_runner()
    monkeypatch.setattr(cr, "_RUNNER_ID", "rid")
    monkeypatch.setattr(cr, "_exchange_runner_proof", lambda rid: ("", 0.0))
    monkeypatch.setattr(cr, "_log", lambda m: None)
    assert cr._bearer() == ""                      # never the PAT
    assert cr._aws_proof_headers("https://x/api/agents/eva/credentials/resolve") == {}


def test_an_invitation_paired_box_has_no_pat_fallback(load_cloud_runner, monkeypatch, tmp_path):
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"runner_id": "rid", "paired_by": "invitation"}))
    monkeypatch.setenv("STATE_FILE", str(state))
    monkeypatch.setenv("CANOPY_TOKEN", "a-persons-pat")
    cr = load_cloud_runner()
    assert cr._bearer() == ""
    state.write_text(json.dumps({"runner_id": "rid"}))   # an old box paired with its PAT keeps it
    cr2 = load_cloud_runner()
    assert cr2._bearer() == "a-persons-pat"


def test_a_runner_signs_in_with_its_own_key_through_the_cli(load_cloud_runner, monkeypatch, tmp_path):
    monkeypatch.setenv("CANOPY_RUNNER_INSTANCE", "dimagi")
    monkeypatch.setenv("CANOPY_DEVICE_KEY", "1")
    monkeypatch.setenv("RUNNER_TOKEN_FILE", str(tmp_path / "rt.json"))
    cr = load_cloud_runner()
    seen = []

    def run(argv, **kw):
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, json.dumps(
            {"access_token": "crt_runner", "expires_at": "2099-01-01T00:00:00Z", "runner": "rid"}), "")

    monkeypatch.setattr(cr.subprocess, "run", run)
    monkeypatch.setattr(cr, "_canopy_cli", lambda: "canopy")
    monkeypatch.setattr(cr, "_RUNNER_ID", "rid")
    monkeypatch.setattr(cr, "_log", lambda m: None)
    assert cr._bearer() == "crt_runner"
    assert seen[0][:5] == ["canopy", "vault", "runner-session", "--runner", "rid"]
    assert json.loads((tmp_path / "rt.json").read_text())["access_token"] == "crt_runner"


def test_a_runner_waits_for_canopy_pair_instead_of_pairing(load_cloud_runner, monkeypatch, tmp_path):
    state = tmp_path / "state.json"
    monkeypatch.setenv("CANOPY_RUNNER_INSTANCE", "dimagi")
    monkeypatch.setenv("STATE_FILE", str(state))
    cr = load_cloud_runner()
    ticks = []

    def sleep(s):
        ticks.append(s)
        state.write_text(json.dumps({"runner_id": "rid-u"}))

    monkeypatch.setattr(cr.time, "sleep", sleep)
    monkeypatch.setattr(cr, "_log", lambda m: None)
    monkeypatch.setattr(cr, "pair_by_role", lambda: pytest.fail("a runner on a shared machine must not pair by role"))
    monkeypatch.setattr(cr, "_api", lambda *a, **k: (200, {}))
    assert cr.pair_or_load() == "rid-u" and ticks


# ---- the template and the systemd runners (lint-style) ----------------------------------

EC2 = pathlib.Path(__file__).resolve().parent.parent


def test_each_runner_runs_as_its_own_user_with_no_privilege(cloud_runner):
    svc = cloud_runner.SYSTEMD_UNITS["canopy-runner@.service"]
    assert "\nUser=cu-%i\n" in svc and "EnvironmentFile=/etc/canopy-runner/%i.env" in svc
    assert "NoNewPrivileges=yes" in svc and "PrivateTmp=yes" in svc and "OOMPolicy=continue" in svc
    assert "Requires=canopy-runners.service" in svc
    # Root's parts stay root's: setup (users, the IMDS rule), the broker, the pairing.
    for name in ("canopy-runners.service", "canopy-kms-broker.service", "canopy-pair.service",
                 "canopy-runner-update.service"):
        assert "User=" not in cloud_runner.SYSTEMD_UNITS[name].replace("User=root", ""), name
    assert "--setup-runners" in cloud_runner.SYSTEMD_UNITS["canopy-runners.service"]
    assert "--pair-runners" in cloud_runner.SYSTEMD_UNITS["canopy-pair.service"]
    assert "kms_broker.py" in cloud_runner.SYSTEMD_UNITS["canopy-kms-broker.service"]


def test_the_template_installs_runners_and_declares_them():
    cfn = (EC2 / "runner.cfn.yaml").read_text()
    assert "\n  Runners:\n    Type: String\n    Default: ''\n" in cfn
    assert "RUNNERS=${Runners}" in cfn
    assert "- [ python3, /opt/canopy-runner/cloud_runner.py, --install-runners ]" in cfn
    # The legacy single runner (User=ubuntu, sudo for the updater) is not built any more.
    assert "canopy-runner.service" not in cfn and "sudoers" not in cfn and "User=ubuntu" not in cfn


def test_the_ecdh_stack_gives_each_extra_runner_its_own_key():
    text = (EC2 / "ecdh-kms.cfn.yaml").read_text()
    # The box's own runner keeps the key it always had (no wrap breaks).
    assert "'alias/canopy-runner-ecdh/${RoleName}'" in text
    for i in range(1, 5):
        assert f"Unit{i}Key:\n    Type: AWS::KMS::Key\n    Condition: HasRunner{i}" in text
        block = text[text.index(f"  Unit{i}Key:"):text.index(f"  Unit{i}KeyAlias:")]
        # A removed runner (or a replaced key) is never scheduled for deletion…
        assert "DeletionPolicy: Retain" in block and "UpdateReplacePolicy: Retain" in block
        # …and its alias (what the broker finds it by) is never removed by the stack.
        alias = text[text.index(f"  Unit{i}KeyAlias:"):][:200]
        assert "DeletionPolicy: Retain" in alias and "UpdateReplacePolicy: Retain" in alias
        assert f"'alias/canopy-runner-ecdh/${{RoleName}}/${{Runner{i}}}'" in text


@pytest.mark.parametrize("template", ["runner.cfn.yaml", "ecdh-kms.cfn.yaml"])
def test_templates_pass_cfn_lint(template):
    pytest.importorskip("cfnlint.api")
    from cfnlint.api import lint_file

    matches = [m for m in lint_file(EC2 / template) if m.rule.id.startswith("E")]
    assert not matches, [str(m) for m in matches]


def test_an_in_place_move_writes_box_env_without_secrets(cloud_runner, tmp_path):
    legacy = tmp_path / "runner.env"
    legacy.write_text("GOG_KEYRING_PASSWORD=pw\nCANOPY_BASE_URL=https://canopy.dimagi.com\nCANOPY_TOKEN=pat\n"
                      "CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-x\nRUNNER_NAME=cloud-ec2-2\nRUNNER_WORKSPACE=dimagi\n"
                      "RUNNER_AGENTS=ace,eva\nAGENT_SLUGS=ace,eva\n")
    out = tmp_path / "etc" / "box.env"
    box = cloud_runner.write_box_env_from_legacy(runners_spec="", legacy=legacy, out=out)
    assert box == {"CANOPY_BASE_URL": "https://canopy.dimagi.com", "RUNNER_AGENTS": "ace,eva",
                   "RUNNER_WORKSPACE": "dimagi", "RUNNER_NAME": "cloud-ec2-2", "AGENT_SLUGS": "ace,eva",
                   "RUNNERS": ""}
    assert (out.stat().st_mode & 0o777) == 0o600
    text = out.read_text()
    assert "pat" not in text and "sk-ant" not in text and "pw" not in text
    # …and it is the live box's one default runner, named as the box.
    [runner] = cloud_runner.parse_runners(box["RUNNERS"], workspace="dimagi", box="cloud-ec2-2")
    assert runner["name"] == "cloud-ec2-2"


def test_install_runners_moves_a_legacy_box_in_place(cloud_runner, monkeypatch, tmp_path):
    """The operator step that moves a live box (README "Moving a machine to several runners"): the
    legacy service is stopped, its secrets are gone before anything is made readable,
    box.env carries no secret, the old fetch-env that re-wrote them is replaced, and
    every runner is enabled."""
    home = tmp_path / "opt" / "canopy-runner"
    home.mkdir(parents=True)
    (home / "runner.env").write_text("CANOPY_TOKEN=pat\nRUNNER_NAME=cloud-ec2-2\nRUNNER_WORKSPACE=dimagi\n"
                                     "GOG_KEYRING_PASSWORD=pw\n")
    (home / "runner-token.json").write_text('{"access_token": "crt_live"}')
    (home / "build-info.json").write_text('{"sha": "abc"}')
    (home / "build-info.json").chmod(0o600)
    fetch_env = tmp_path / "canopy-fetch-env"
    fetch_env.write_text("#!/bin/bash\ncat > /opt/canopy-runner/runner.env <<ENV\nCANOPY_TOKEN=...\nENV\n")
    sudoers = tmp_path / "sudoers"
    sudoers.write_text("ubuntu ALL=(root) NOPASSWD: ...")
    systemd = tmp_path / "systemd"
    systemd.mkdir()
    etc = tmp_path / "etc"
    monkeypatch.setattr(cloud_runner, "RUNNER_HOME", home)
    monkeypatch.setattr(cloud_runner, "BOX_ENV_FILE", etc / "box.env")
    monkeypatch.setattr(cloud_runner, "install_broker", lambda run=None: True)
    monkeypatch.setattr(cloud_runner, "load_runners",
                        lambda path=None: {"runners": [{"id": "dimagi"}, {"id": "agent-eva"}]})
    monkeypatch.setenv("RUNNERS", "tree:dimagi,agent:eva")
    calls = []
    services = cloud_runner.install_runners(run=lambda argv, **kw: calls.append(argv), systemd=systemd,
                                          fetch_env=fetch_env, sudoers=sudoers)
    assert services == ["canopy-runner@dimagi.service", "canopy-runner@agent-eva.service"]
    assert ["systemctl", "disable", "--now", "canopy-runner.service"] in calls
    assert not (home / "runner.env").exists() and not (home / "runner-token.json").exists()
    assert not sudoers.exists()
    assert "runner.env" not in fetch_env.read_text().split("exit 0")[-1] and "exit 0" in fetch_env.read_text()
    box = cloud_runner.read_env_file(etc / "box.env")
    assert box == {"RUNNER_NAME": "cloud-ec2-2", "RUNNER_WORKSPACE": "dimagi", "RUNNERS": "tree:dimagi,agent:eva"}
    assert (home / "build-info.json").stat().st_mode & 0o777 == 0o644   # every runner reads its provenance
    assert sorted(p.name for p in systemd.iterdir()) == sorted(cloud_runner.SYSTEMD_UNITS)
    enable = next(c for c in calls if c[:3] == ["systemctl", "enable", "--now"] and "canopy-pair.service" in c)
    assert "canopy-runner@agent-eva.service" in enable and "canopy-kms-broker.service" in enable
