"""Secrets shared with a chat (`models.SessionSecret`).

The contract: a person hands a value to one chat without it entering the chat,
canopy-web never holds it in a form it can read (the browser seals it, `zks1`,
to the device keys of the machine running the chat), and ONLY the session
driving that chat gets the sealed value. That session proves which chat it is
with the chat's KEY (`ChatKey`); there is no route that takes a chat id. A
secret dies 30 minutes after it was shared.
"""
from __future__ import annotations

import datetime as dt
import json

import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from django.contrib.auth.models import User
from django.test import Client
from django.utils import timezone

from apps.agents.credentials import zk_seal
from apps.agents.models import Agent
from apps.canopy_sessions import secrets as session_secrets
from apps.canopy_sessions.models import Message, RunnerBinding, Session, SessionSecret
from apps.harness import device_keys
from apps.harness.models import Runner, RunnerDeviceKey
from apps.passkeys.testing import grant_step_up
from apps.workspaces.models import Workspace, WorkspaceMembership
from tests.fixtures.vault_zk import ref

pytestmark = pytest.mark.django_db

TOKEN = "ghp_not_a_real_token_0123456789"
MINE = "eb742bd8-0000-0000-0000-00000000000a"    # this conversation's Claude session id
OTHER = "eb742bd8-0000-0000-0000-00000000000b"   # another conversation of the same agent


def _pat(user) -> str:
    from apps.tokens.models import PersonalToken

    raw, _ = PersonalToken.create_for_user(user=user, label="test")
    return raw


def _device(runner, *, approved=True, status=RunnerDeviceKey.ACTIVE):
    """A device key on `runner` with a real P-256 ECDH key; returns (row, private key)."""
    priv = ec.generate_private_key(ec.SECP256R1())
    jwk = ref.jwk(priv.public_key())
    row = RunnerDeviceKey.objects.create(
        runner=runner, kind=RunnerDeviceKey.SOFTWARE, status=status,
        jkt=f"sig-{RunnerDeviceKey.objects.count()}-{runner.pk}", public_jwk={},
        ecdh_public_jwk=jwk, ecdh_jkt=device_keys.thumbprint(jwk),
        ecdh_approved_at=timezone.now() if approved else None)
    return row, priv


@pytest.fixture()
def world():
    owner = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=owner)
    WorkspaceMembership.objects.create(user=owner, workspace=ws, role=WorkspaceMembership.OWNER)
    bot = User.objects.create_user("hal", "hal@dimagi-ai.com", "pw")
    agent = Agent.objects.create(slug="hal", name="Hal", workspace=ws, user=bot)
    runner = Runner.objects.create(
        name="jj-mbp", kind=Runner.EMDASH, host="jj-mac", owner=owner, workspace=ws,
        status=Runner.ONLINE, last_heartbeat_at=timezone.now(),
    )
    device, priv = _device(runner)

    def chat(transcript_id, key):
        s = Session.objects.create(workspace=ws, created_by=owner, origin=Session.ORIGIN_WEB, agent=agent)
        RunnerBinding.objects.create(session=s, runner=runner, session_key=key,
                                     emdash_project="hal", transcript_id=transcript_id)
        return s

    session = chat(MINE, "task-a")
    other = chat(OTHER, "task-b")
    browser = Client()
    browser.force_login(owner)
    grant_step_up(browser)
    from apps.canopy_sessions import chat_keys

    return {"owner": owner, "ws": ws, "bot": bot, "agent": agent, "runner": runner,
            "device": device, "priv": priv,
            "session": session, "other": other, "browser": browser,
            "keys": {"mine": chat_keys.mint(session), "other": chat_keys.mint(other)}}


def _recipients(world, session=None, client=None):
    s = session or world["session"]
    return (client or world["browser"]).get(f"/api/canopy-sessions/{s.id}/secrets/recipients")


def _seal(world, name, value, session=None):
    """What the browser does: seal to the keys the recipients route names."""
    r = _recipients(world, session).json()
    return zk_seal.seal_to(value, agent_id=r["binding_id"], name=name, kv=r["kv"],
                           recipients=[(x["kind"], x["target"], x["public_jwk"]) for x in r["recipients"]])


def _post(world, body, session=None, client=None):
    s = session or world["session"]
    return (client or world["browser"]).post(f"/api/canopy-sessions/{s.id}/secrets",
                                             data=json.dumps(body), content_type="application/json")


def _share(world, name="GH_TOKEN", value=TOKEN, session=None, client=None):
    return _post(world, {"name": name, "value_zk": _seal(world, name, value, session)}, session, client)


def _agent(world, user=None):
    return Client(HTTP_AUTHORIZATION=f"Bearer {_pat(user or world['bot'])}")


def _listed(world, chat="mine", user=None):
    return _agent(world, user).get("/api/session-secrets/key",
                                   HTTP_X_CANOPY_CHAT_KEY=world["keys"].get(chat, chat))


def _value(world, chat="mine", name="GH_TOKEN", user=None, sealed=True):
    return _agent(world, user).get(f"/api/session-secrets/key/{name}" + ("?sealed=true" if sealed else ""),
                                   HTTP_X_CANOPY_CHAT_KEY=world["keys"].get(chat, chat))


def _open(world, body, priv=None, device=None):
    """What `canopy secret exec` does on the runner: open its own entry with its ECDH key."""
    d = device or world["device"]
    return ref.open_sealed_value(body["value_zk"], body["binding_id"], body["name"], "device", d.ecdh_jkt,
                                 priv or world["priv"])


# ---- who it is sealed to -------------------------------------------------------

def test_recipients_are_the_bound_runners_approved_ecdh_keys(world):
    _device(world["runner"], approved=False)                       # ECDH key not approved yet
    _device(world["runner"], status=RunnerDeviceKey.REVOKED)       # revoked
    r = _recipients(world)
    assert r.status_code == 200, r.content
    body = r.json()
    assert body["binding_id"] == f"session:{world['session'].id}"
    assert body["kv"] == 0 and body["refusal"] is None
    assert body["runner_name"] == "jj-mbp"
    assert [x["target"] for x in body["recipients"]] == [world["device"].ecdh_jkt]
    assert body["recipients"][0]["public_jwk"] == world["device"].ecdh_public_jwk


def test_a_chat_on_no_machine_is_refused_with_a_sentence(world):
    RunnerBinding.objects.filter(session=world["session"]).update(runner=None)
    body = _recipients(world).json()
    assert body["recipients"] == [] and body["error"] == "no_runner"
    assert "not running on a machine yet" in body["refusal"]
    sealed = zk_seal.seal_to(TOKEN, agent_id=body["binding_id"], name="GH_TOKEN", kv=0,
                             recipients=[("device", world["device"].ecdh_jkt, world["device"].ecdh_public_jwk)])
    r = _post(world, {"name": "GH_TOKEN", "value_zk": sealed})
    assert r.status_code == 409 and r.json()["error"] == "no_runner"
    assert not SessionSecret.objects.exists()


def test_a_runner_with_no_approved_ecdh_key_is_refused_with_a_sentence(world):
    RunnerDeviceKey.objects.update(ecdh_approved_at=None)
    body = _recipients(world).json()
    assert body["error"] == "runner_has_no_ecdh_key"
    assert "jj-mbp" in body["refusal"] and "no approved encryption key" in body["refusal"]


def test_recipients_need_write_access_to_the_chat(world):
    stranger = User.objects.create_user("x", "x@elsewhere.org", "pw")
    c = Client()
    c.force_login(stranger)
    assert _recipients(world, client=c).status_code == 404


# ---- sharing -----------------------------------------------------------------

def test_sharing_stores_only_the_sealed_value_and_posts_nothing_into_the_chat(world):
    r = _share(world)
    assert r.status_code == 201, r.content
    assert r.json()["name"] == "GH_TOKEN"
    stored = SessionSecret.objects.get(name="GH_TOKEN").value_zk
    assert stored.startswith("zks1:") and TOKEN not in stored
    assert not Message.objects.filter(session=world["session"]).exists()


def test_plaintext_is_refused(world):
    r = _post(world, {"name": "GH_TOKEN", "value": TOKEN})
    assert r.status_code == 422 and r.json()["error"] == "sealed_only"
    r = _post(world, {"name": "GH_TOKEN", "value_zk": TOKEN})
    assert r.status_code == 422 and r.json()["error"] == "sealed_only"
    assert not SessionSecret.objects.exists()


def test_a_value_sealed_to_a_key_the_chats_machine_does_not_hold_is_refused(world):
    elsewhere = Runner.objects.create(name="other-box", kind=Runner.EMDASH, owner=world["owner"],
                                      workspace=world["ws"])
    stray, _ = _device(elsewhere)
    sealed = zk_seal.seal_to(TOKEN, agent_id=f"session:{world['session'].id}", name="GH_TOKEN", kv=0,
                             recipients=[("device", stray.ecdh_jkt, stray.ecdh_public_jwk)])
    r = _post(world, {"name": "GH_TOKEN", "value_zk": sealed})
    assert r.status_code == 409 and r.json()["error"] == "stale_recipients"


def test_the_browser_list_shows_names_and_never_a_value(world):
    _share(world)
    r = world["browser"].get(f"/api/canopy-sessions/{world['session'].id}/secrets")
    assert [s["name"] for s in r.json()] == ["GH_TOKEN"]
    assert r.json()[0]["expires_at"]
    assert "zks1" not in r.content.decode()


def test_there_is_no_route_that_returns_a_value_by_chat_id(world):
    _share(world)
    r = _agent(world).get(f"/api/canopy-sessions/{world['session'].id}/secrets/GH_TOKEN/value")
    assert r.status_code in (404, 405)


def test_names_are_env_var_shaped_and_bound_by_the_seal(world):
    # The seal binds the name, so canopy-web cannot reshape it: the sharer does.
    r = _share(world, name="my-token")
    assert r.status_code == 422 and "MY_TOKEN" in r.content.decode()
    assert _share(world, name="9LIVES").status_code == 422
    assert session_secrets.normalize_name("gh token") == "GH_TOKEN"


# ---- only the session holding the chat's key ---------------------------------

def test_the_keyed_session_gets_the_sealed_value_and_its_machine_opens_it(world):
    _share(world)
    listed = _listed(world)
    assert listed.status_code == 200
    assert [s["name"] for s in listed.json()] == ["GH_TOKEN"]
    r = _value(world)
    assert r.status_code == 200, r.content
    body = r.json()
    assert body["binding_id"] == f"session:{world['session'].id}" and body["kv"] == 0
    assert TOKEN not in r.content.decode()
    assert _open(world, body) == TOKEN
    assert SessionSecret.objects.get(name="GH_TOKEN").last_used_at is not None


def test_another_machines_key_cannot_open_it(world):
    _share(world)
    _, stranger = _device(Runner.objects.create(name="b", kind=Runner.EMDASH, owner=world["owner"],
                                                 workspace=world["ws"]))
    with pytest.raises(Exception):
        _open(world, _value(world).json(), priv=stranger)


def test_an_older_cli_asking_for_plaintext_is_told_to_update(world):
    _share(world)
    r = _value(world, sealed=False)
    assert r.status_code == 409 and r.json()["error"] == "update_canopy"
    assert "update canopy" in r.json()["detail"]
    assert TOKEN not in r.content.decode()


def test_reading_is_audited_by_name_only(world):
    from apps.events.models import Event

    _share(world)
    _value(world)
    ev = Event.objects.get(kind="session.secret.resolved")
    assert ev.payload["name"] == "GH_TOKEN"
    assert TOKEN not in json.dumps(ev.payload) and "zks1" not in json.dumps(ev.payload)


def test_another_chat_of_the_same_agent_cannot_see_or_spend_it(world):
    _share(world)
    assert _listed(world, "other").json() == []
    assert _value(world, "other").status_code == 404


def test_each_key_sees_only_its_own_chats_secrets(world):
    _share(world, name="MINE_ONLY")
    _share(world, name="THEIRS_ONLY", session=world["other"])
    assert [s["name"] for s in _listed(world).json()] == ["MINE_ONLY"]
    assert [s["name"] for s in _listed(world, "other").json()] == ["THEIRS_ONLY"]


def test_no_key_or_a_wrong_key_gets_a_404(world):
    _share(world)
    assert _agent(world).get("/api/session-secrets/key/GH_TOKEN?sealed=true").status_code == 404
    assert _value(world, "chk_not-a-key").status_code == 404


def test_the_agent_login_alone_is_not_enough(world):
    """Neither identity nor a session id reaches a secret; only the key."""
    _share(world)
    assert _agent(world).get(f"/api/session-secrets/{MINE}").status_code == 404
    assert _agent(world).get(f"/api/session-secrets/{MINE}/GH_TOKEN").status_code == 404


def test_a_browser_session_is_refused_even_for_the_sharer(world):
    _share(world)
    r = world["browser"].get("/api/session-secrets/key/GH_TOKEN?sealed=true",
                             HTTP_X_CANOPY_CHAT_KEY=world["keys"]["mine"])
    assert r.status_code == 403


def test_a_browser_with_a_junk_bearer_header_is_still_refused(world):
    """The header alone is not a token: the cookie signed the request in, the
    junk bearer authenticated nothing, so this is a browser and gets nothing."""
    _share(world)
    for path in ("/api/session-secrets/key", "/api/session-secrets/key/GH_TOKEN?sealed=true"):
        r = world["browser"].get(path, HTTP_X_CANOPY_CHAT_KEY=world["keys"]["mine"],
                                 HTTP_AUTHORIZATION="Bearer junk")
        assert r.status_code == 403, (path, r.content)


def test_forgetting_a_secret_removes_it(world):
    _share(world)
    r = world["browser"].delete(f"/api/canopy-sessions/{world['session'].id}/secrets/GH_TOKEN")
    assert r.status_code == 204
    assert _value(world).status_code == 404


def test_a_row_left_from_before_sealing_is_not_there(world):
    """The deploy drops the Fernet column; a row it leaves has no sealed value."""
    SessionSecret.objects.create(session=world["session"], name="OLD", value_zk="")
    assert _listed(world).json() == []
    assert _value(world, name="OLD").status_code == 404


# ---- 30-minute lifetime ------------------------------------------------------

def _age(minutes):
    SessionSecret.objects.update(updated_at=timezone.now() - dt.timedelta(minutes=minutes))


def test_an_expired_secret_is_refused_and_deleted(world):
    _share(world)
    _age(31)
    assert _value(world).status_code == 404
    assert not SessionSecret.objects.exists()


def test_a_secret_inside_its_window_still_works(world):
    _share(world)
    _age(29)
    assert _value(world).status_code == 200


def test_both_lists_drop_expired_secrets(world):
    _share(world)
    _age(31)
    assert world["browser"].get(f"/api/canopy-sessions/{world['session'].id}/secrets").json() == []
    _share(world)
    _age(31)
    assert _listed(world).json() == []


def test_resharing_restarts_the_clock(world):
    _share(world)
    _age(29)
    _share(world, value="a-new-value-for-the-same-name")
    # 29 min after the FIRST share + 2 more would be past 30 had the clock not restarted.
    assert session_secrets.purge_expired(timezone.now() + dt.timedelta(minutes=2)) == 0
    assert _open(world, _value(world).json()) == "a-new-value-for-the-same-name"


def test_the_off_request_clock_sweeps_expired_secrets(world):
    from apps.common import ticks

    _share(world)
    _age(31)
    ticks.jobs()["canopy_sessions.purge_expired_secrets"].fn()
    assert not SessionSecret.objects.exists()


def test_a_browser_shares_only_after_a_passkey_step_up(world):
    # The SPA's API client answers `step_up_required` with Touch ID and a retry.
    fresh = Client()
    fresh.force_login(world["owner"])
    res = _post(world, {"name": "GH_TOKEN", "value_zk": _seal(world, "GH_TOKEN", TOKEN)}, client=fresh)
    assert res.status_code == 403 and res.json()["error"] == "step_up_required"
    assert not SessionSecret.objects.exists()


def test_a_token_shares_without_a_step_up(world):
    res = _share(world, client=Client(HTTP_AUTHORIZATION=f"Bearer {_pat(world['owner'])}"))
    assert res.status_code == 201
