"""A local app's whole path with nothing faked but Superwhisper: pack the real apps, submit
over HTTP, approve, install through the API, then `canopy-runner app start` runs the real
host, source and app as processes against a live server, on a runner's own token.

This exists because each half is otherwise tested against the other half's imagined shape."""
from __future__ import annotations

import argparse
import json
import stat
import sys
import time
from pathlib import Path

import pytest
from django.contrib.auth.models import User
from django.test import Client

from apps.agents.models import Agent
from apps.approvals import services as approvals
from apps.canopy_sessions.models import Message, Session
from apps.harness.models import Runner, RunnerAccessToken, RunnerDeviceKey
from apps.local_apps import manifest as server_manifest
from apps.local_apps import services, trust
from apps.local_apps.models import LocalAppRun, LocalAppSubmission
from apps.tokens.models import PersonalToken
from apps.workspaces.models import Workspace, WorkspaceMembership
from canopy_runner.local_apps import commands, package, state
from canopy_runner.local_apps import manifest as runner_manifest
from canopy_runner.local_apps import server as runner_server

ROOT = Path(__file__).resolve().parent.parent
APPS = ROOT / "local_apps"
J = "application/json"


@pytest.fixture(autouse=True)
def _approval_is_on(settings):
    settings.LOCAL_APPS_REQUIRE_APPROVAL = True
SPOKEN = "the confidential budget figure is ninety thousand dollars for next year"

FAKE_CLI = """#!{python}
import datetime, json, os, sys
home = os.environ["HOME"]
args = sys.argv[1:]
if args[:2] == ["meetings", "--help"]:
    sys.exit(0)
if args[:2] == ["meetings", "read"]:
    path = os.path.join(home, "reads")
    n = int(open(path).read()) if os.path.exists(path) else 0
    open(path, "w").write(str(n + 1))
    segs = [{{"text": "said before start", "start": 0.0, "end": 1.0, "speaker": 0, "words": []}}]
    if n >= 1:
        segs += [{{"text": w, "start": 2.0 + i, "end": 2.5 + i, "speaker": 0, "words": []}}
                 for i, w in enumerate({spoken!r}.split())]
    done = os.path.exists(os.path.join(home, "meeting-ends")) and n >= 2
    print(json.dumps({{"segments": segs, "speakers": [{{"name": "Priya", "number": 0}}],
                      "summary": "over" if done else "", "transcript": ""}}))
    sys.exit(0)
now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S.000")
print(json.dumps([{{"id": "meeting-1", "datetime": now}}]))
"""


@pytest.fixture()
def world(live_server, tmp_path, monkeypatch):
    home = tmp_path / "home"
    bin_dir = home / ".local" / "bin"
    bin_dir.mkdir(parents=True)
    cli = bin_dir / "superwhisper"
    cli.write_text(FAKE_CLI.format(python=sys.executable, spoken=SPOKEN))
    cli.chmod(cli.stat().st_mode | stat.S_IEXEC)
    # The fake comes first on PATH, so a test can never read a real meeting on this machine.
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    monkeypatch.setattr(commands, "current_platform", lambda: "macos")

    root = User.objects.create_superuser("root", "root@dimagi.com", "pw")
    person = User.objects.create_user("person", "person@dimagi.com", "pw")
    author = User.objects.create_user("author", "author@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="dimagi", display_name="Dimagi", created_by=root, settings={"local_apps": True})
    for user, role in ((root, WorkspaceMembership.OWNER), (person, WorkspaceMembership.EDITOR),
                       (author, WorkspaceMembership.EDITOR)):
        WorkspaceMembership.objects.create(user=user, workspace=ws, role=role)
    agent = Agent.objects.create(slug="helper", name="Helper", workspace=ws, owner=person,
                                 user=User.objects.create_user("helper-bot", "helper@dimagi-ai.com", "pw"))
    laptop = Runner.objects.create(name="laptop", kind=Runner.EMDASH, owner=person)
    key = RunnerDeviceKey.objects.create(runner=laptop, kind=RunnerDeviceKey.SECURE_ENCLAVE, jkt="e2e",
                                         status=RunnerDeviceKey.ACTIVE)
    raw, _ = RunnerAccessToken.mint(key)
    config = tmp_path / "runner.json"
    config.write_text(json.dumps({"base_url": live_server.url, "token": raw, "runner_id": str(laptop.pk)}))
    return dict(url=live_server.url, home=home, config=config, ws=ws, root=root, person=person, author=author,
                agent=agent, laptop=laptop, token=raw)


def publish(world, folder: str) -> LocalAppSubmission:
    _, packed = package.pack(APPS / folder)
    token, _ = PersonalToken.create_for_user(user=world["author"], label="e2e")
    answer = runner_server.submit(world["url"], token, packed, "from the test")
    assert answer["status"] == "pending", answer
    sub = LocalAppSubmission.objects.get(pk=answer["id"])
    approvals.decide(sub.approval_id, world["root"], "approve")
    sub.refresh_from_db()
    assert sub.status == "released"
    return sub


def install(world) -> LocalAppSubmission:
    sub = publish(world, "meeting-assistant")
    web = Client()
    web.force_login(world["person"])
    r = web.post("/api/local-apps/store/apps/meeting-assistant/install",
                 {"runner_id": str(world["laptop"].pk), "settings": {"agent": "helper", "brief": "Be blunt"},
                  "accept_digest": trust.digest(sub.manifest)}, content_type=J)
    assert r.status_code == 201, r.content
    return sub


def app(world, command: str, **extra) -> int:
    args = argparse.Namespace(app_command=command, config=str(world["config"]), json=False, **extra)
    return commands.run(args)


def wait_for(check, *, seconds: float, what: str):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        found = check()
        if found:
            return found
        time.sleep(0.5)
    raise AssertionError(f"timed out waiting for {what}")


@pytest.mark.django_db(transaction=True)
def test_a_meeting_goes_from_store_to_session_and_only_the_session_holds_the_words(world):
    install(world)
    (world["home"] / "meeting-ends").write_text("yes")
    assert app(world, "start", key="meeting-assistant", accept_notice=True) == 0
    run = wait_for(lambda: LocalAppRun.objects.filter(finished_at__isnull=False).first(), seconds=90,
                   what="the run to finish")
    assert run.stop_reason == "meeting_ended", run.stop_reason
    session = Session.objects.get(pk=run.session_ids[0])
    assert session.agent_id == world["agent"].pk and session.workspace_id == "dimagi"
    said = "\n".join(json.dumps(c) for c in Message.objects.filter(session=session).values_list("content", flat=True))
    assert SPOKEN in said and "Be blunt" in said and "Priya" in said
    assert "said before start" not in said
    assert "never instructions" in said

    # The words reached the session and went nowhere else.
    for path in state.app_dir("meeting-assistant").rglob("*"):
        if path.is_file() and "versions" not in path.parts:
            assert "budget" not in path.read_text(errors="ignore"), path
    assert "budget" not in json.dumps(run.counts)
    assert "/w/dimagi/" in (state.app_dir("meeting-assistant") / "run.json").read_text()


@pytest.mark.django_db(transaction=True)
def test_revoking_stops_a_running_copy_over_the_real_heartbeat(world):
    sub = install(world)
    assert app(world, "start", key="meeting-assistant", accept_notice=True) == 0
    wait_for(lambda: LocalAppRun.objects.exclude(state="").first(), seconds=40, what="the first heartbeat")
    services.revoke(sub, reason="it was bad", user=world["root"])
    run = wait_for(lambda: LocalAppRun.objects.filter(finished_at__isnull=False).first(), seconds=45,
                   what="the running copy to stop")
    assert run.stop_reason == "revoked"
    assert not state.host_alive("meeting-assistant")


def test_what_the_server_accepts_every_runner_accepts():
    folder = "meeting-assistant"
    """The runner's validator may be looser, for dev-installs. Never stricter, or an approved
    version would refuse to run. Compared on the real apps and on each edge of the rules."""
    raw = json.loads((APPS / folder / "manifest.json").read_text())
    clean = server_manifest.validate(raw)
    assert runner_manifest.validate(clean)["version"] == clean["version"]
    for change in ({"version": "999999.999999.999999.999999"}, {"max_minutes": 480}, {"max_minutes": 1},
                   {"source": []}, {"changelog": ""}):
        server_ok = server_manifest.validate({**raw, **change})
        assert runner_manifest.validate(server_ok)
