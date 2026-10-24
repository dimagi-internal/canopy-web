"""An agent login's TEST vault key (`apps/agents/credentials/login_test_key.py`).

Off by default. An agent admin turns it on from a browser behind a passkey step-up;
the agent's own login then makes ONE passphrase-sealed vault key, which receives only
copies of the keys of sensitive conversations that login is itself in — never a vault
key, an endorsement, a device approval or a passkey. Turning it off deletes the key
and every conversation-key copy wrapped to it.
"""
from __future__ import annotations

import os
import types

import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from django.contrib.auth.models import User
from django.test import Client

from apps.agents.credentials import login_test_key, vault_engine, workspace_vaults, zk, zk_endorse
from apps.agents.models import AgentAdmin
from apps.canopy_sessions import conversation_keys
from apps.canopy_sessions.models import Session, SessionKeyWrap, SessionParticipant
from apps.common import ecdh_es
from apps.events.models import SettingChange
from apps.passkeys.models import Passkey, UserVaultKeyset
from apps.passkeys.testing import grant_step_up
from apps.tokens.models import PersonalToken
from apps.workspaces.models import SystemAccount, WorkspaceMembership
from tests.fixtures.sensitive import SENSITIVE, rewrap_for, unwrap, world
from tests.fixtures.vault_zk import ref

pytestmark = pytest.mark.django_db

PASSPHRASE = "a test passphrase for a harness"


@pytest.fixture()
def w():
    x = world()
    login = User.objects.create_user("labsdata-login", "labsdata@dimagi-ai.com", "pw")
    WorkspaceMembership.objects.create(user=login, workspace=x["ws"], role=WorkspaceMembership.EDITOR)
    x["agent"].user = login
    x["agent"].save(update_fields=["user"])
    raw, _ = PersonalToken.create_for_user(user=login, label="harness")
    x["login"], x["login_pat"] = login, raw
    grant_step_up(x["c"])
    return x


def _bearer(raw):
    return {"HTTP_AUTHORIZATION": f"Bearer {raw}"}


def _url(w):
    return f"/api/agents/{w['agent'].slug}/test-vault-key"


def _turn_on(w):
    r = w["c"].put(_url(w), {"enabled": True}, content_type="application/json")
    assert r.status_code == 200, r.content
    return r.json()


def _passphrase_keyset(user, *, prf=False):
    """What a harness's browser (or the CLI) makes: a key pair, its private scalar sealed
    with a passphrase + Secret Key (2SKD). Returns (body, private key)."""
    priv = ec.generate_private_key(ec.SECP256R1())
    scalar = priv.private_numbers().private_value.to_bytes(32, "big")
    seals = [ref.seal_user_key_2skd(scalar, str(user.pk), PASSPHRASE, os.urandom(16), os.urandom(16),
                                    os.urandom(12))]
    if prf:
        seals.append(ref.seal_user_key(scalar, str(user.pk), ref.b64u(b"cred"), os.urandom(32),
                                       os.urandom(16), os.urandom(12)))
    return {"public_jwk": ref.jwk(priv.public_key()), "seals": seals}, priv


def _make_key(w, **kw):
    body, priv = _passphrase_keyset(w["login"], **kw)
    r = Client().put("/api/vault/keyset", body, content_type="application/json", **_bearer(w["login_pat"]))
    return r, priv


# --- the switch ---------------------------------------------------------------------


def test_it_is_off_by_default_and_the_login_cannot_make_a_key(w):
    assert w["c"].get(_url(w)).json()["enabled"] is False
    r, _ = _make_key(w)
    assert r.status_code == 403 and r.json()["error"] == "not_a_person"
    assert not UserVaultKeyset.objects.filter(user=w["login"]).exists()


def test_only_an_admin_at_a_browser_after_a_step_up_turns_it_on(w):
    # A token cannot, the owner's included.
    owner_pat, _ = PersonalToken.create_for_user(user=w["user"], label="t")
    r = Client().put(_url(w), {"enabled": True}, content_type="application/json", **_bearer(owner_pat))
    assert r.status_code == 403
    # A browser with no fresh step-up is asked for one.
    fresh = Client()
    fresh.force_login(w["user"])
    r = fresh.put(_url(w), {"enabled": True}, content_type="application/json")
    assert r.status_code == 403 and r.json()["error"] == "step_up_required"
    # An editor who is not an admin of the agent cannot.
    sam = User.objects.create_user("sam", "sam@dimagi.com", "pw")
    WorkspaceMembership.objects.create(user=sam, workspace=w["ws"], role=WorkspaceMembership.EDITOR)
    c = Client()
    c.force_login(sam)
    grant_step_up(c)
    assert c.put(_url(w), {"enabled": True}, content_type="application/json").status_code == 403
    w["agent"].refresh_from_db()
    assert w["agent"].test_vault_key is False
    # The owner at a browser, stepped up, can — and it is audited as a setting.
    assert _turn_on(w)["enabled"] is True
    change = SettingChange.objects.filter(path__endswith="/test-vault-key").latest("id")
    assert "test-vault-key" in str(change.changes)


def test_an_agent_login_that_is_its_own_admin_still_cannot_turn_it_on(w):
    AgentAdmin.objects.create(agent=w["agent"], user=w["login"], granted_by=w["user"])
    r = Client().put(_url(w), {"enabled": True}, content_type="application/json", **_bearer(w["login_pat"]))
    assert r.status_code == 403


def test_an_agent_with_no_login_has_nothing_to_turn_on(w):
    w["agent"].user = None
    w["agent"].save(update_fields=["user"])
    r = w["c"].put(_url(w), {"enabled": True}, content_type="application/json")
    assert r.status_code == 409 and r.json()["error"] == "no_agent_login"


# --- making the key -----------------------------------------------------------------


def test_with_it_on_the_login_makes_one_passphrase_key_without_a_step_up(w):
    _turn_on(w)
    r, _ = _make_key(w)
    assert r.status_code == 200, r.content
    assert r.json()["test_key"] is True
    me = Client().get("/api/vault/keyset", **_bearer(w["login_pat"])).json()
    assert me["test_key"] is True and me["exists"] is True
    state = w["c"].get(_url(w)).json()
    assert state["has_key"] and state["key_jkt"] == ecdh_es.jkt(me["public_jwk"])
    # A person's keyset is never marked a test key.
    assert w["c"].get("/api/vault/keyset").json()["test_key"] is False


def test_a_test_key_is_never_sealed_with_a_passkey(w):
    _turn_on(w)
    Passkey.objects.create(user=w["login"], credential_id=ref.b64u(b"cred"), public_key=b"x", prf_enabled=True)
    r, _ = _make_key(w, prf=True)
    assert r.status_code == 422 and r.json()["error"] == "test_key_passphrase_only"


def test_a_test_key_is_made_once_and_never_replaced(w):
    _turn_on(w)
    assert _make_key(w)[0].status_code == 200
    body, _ = _passphrase_keyset(w["login"])
    for replace in (False, True):
        r = Client().put("/api/vault/keyset", {**body, "replace": replace}, content_type="application/json",
                         **_bearer(w["login_pat"]))
        assert r.status_code == 409 and r.json()["error"] == "test_key_exists"


def test_a_system_account_never_makes_a_key(w):
    alarms = User.objects.create_user("alarms", "alarms@dimagi.com", "pw")
    SystemAccount.objects.create(workspace=w["ws"], user=alarms, name="alarms")
    c = Client()
    c.force_login(alarms)
    grant_step_up(c)
    body, _ = _passphrase_keyset(alarms)
    r = c.put("/api/vault/keyset", body, content_type="application/json")
    assert r.status_code == 403 and r.json()["error"] == "not_a_person"
    with pytest.raises(zk.ZkError) as e:
        zk.put_keyset(alarms, public_jwk=body["public_jwk"], seals=body["seals"])
    assert e.value.code == "not_a_person"


def test_the_setup_screen_is_never_asked_of_an_agent_login(w):
    c = Client()
    c.force_login(w["login"])
    assert c.get("/api/vault/setup").json()["required"] is False


# --- nothing in any vault -----------------------------------------------------------


def test_an_agent_login_never_steps_up_or_registers_a_passkey(w):
    c = Client()
    c.force_login(w["login"])
    for path in ("/api/passkeys/register/begin", "/api/passkeys/step-up/begin"):
        r = c.post(path)
        assert r.status_code == 403 and r.json()["error"] == "not_a_person", path


def test_an_agent_login_never_approves_a_device_key(w):
    """Approving a machine is a step-up act, and a step-up is a person's — even a
    session that somehow carries a step-up mark is refused."""
    from apps.harness.models import RunnerDeviceKey

    key = RunnerDeviceKey.objects.create(runner=w["runner"], jkt="j" * 43, public_jwk={"kty": "EC"},
                                         kind="secure-enclave", status=RunnerDeviceKey.PENDING)
    w["runner"].owner = w["login"]
    w["runner"].save(update_fields=["owner"])
    c = Client()
    c.force_login(w["login"])
    grant_step_up(c)
    r = c.post(f"/api/runner-device-keys/{key.pk}/approve")
    assert r.status_code == 403 and r.json()["error"] == "not_a_person"
    key.refresh_from_db()
    assert key.status == RunnerDeviceKey.PENDING


def _member_wrap(agent_id, user, pub):
    return types.SimpleNamespace(kind="member", target=str(user.pk), wrap=ref.wrap_ecdh_es(
        os.urandom(32), pub, ref.member_info(agent_id, 1, str(user.pk)),
        ec.generate_private_key(ec.SECP256R1()), os.urandom(12)))


def test_no_agent_vault_wrap_ever_goes_to_a_test_key(w):
    _turn_on(w)
    _, priv = _make_key(w)
    store = zk._store(w["agent"])
    with pytest.raises(zk.ZkError) as e:
        vault_engine.parse_wraps(store, 1, [_member_wrap(str(w["agent"].pk), w["login"], priv.public_key())],
                                 allow_recovery=True)
    assert e.value.code == "not_a_person"


def test_no_agent_vault_wrap_goes_to_it_even_if_it_were_made_a_member(w):
    """Eligibility already leaves it out; the refusal does not depend on that."""
    _turn_on(w)
    _, priv = _make_key(w)
    store = zk._store(w["agent"])
    store.eligible_members = lambda: [UserVaultKeyset.objects.get(user=w["login"])]
    with pytest.raises(zk.ZkError) as e:
        vault_engine.parse_wraps(store, 1, [_member_wrap(str(w["agent"].pk), w["login"], priv.public_key())],
                                 allow_recovery=True)
    assert e.value.code == "not_a_person"


def test_the_agent_vault_routes_refuse_a_wrap_to_it(w):
    _turn_on(w)
    _, priv = _make_key(w)
    body = {"kv": 1, "values": [],
            "wraps": [{"kind": "member", "target": str(w["login"].pk),
                       "wrap": _member_wrap(str(w["agent"].pk), w["login"], priv.public_key()).wrap}]}
    r = w["c"].post(f"/api/agents/{w['agent'].slug}/vault/zk/enable", body, content_type="application/json")
    assert r.status_code == 403 and r.json()["error"] == "not_a_person", r.content


def test_no_workspace_or_runner_vault_wrap_ever_goes_to_a_test_key(w):
    _turn_on(w)
    _, priv = _make_key(w)
    vault = workspace_vaults.create(w["ws"], "shared", by=w["user"])
    with pytest.raises(zk.ZkError) as e:
        vault_engine.parse_wraps(workspace_vaults._store(vault), 1,
                                 [_member_wrap(f"vault:{vault.pk}", w["login"], priv.public_key())],
                                 allow_recovery=True)
    assert e.value.code == "not_a_person"


def test_no_value_is_sealed_to_a_test_key(w):
    _turn_on(w)
    _, priv = _make_key(w)
    sealed = ref.seal_value("s3cret", str(w["agent"].pk), "TOKEN", 0,
                            [("member", str(w["login"].pk), priv.public_key())],
                            [ec.generate_private_key(ec.SECP256R1())], [os.urandom(12)])
    store = zk._store(w["agent"])
    with pytest.raises(zk.ZkError) as e:
        vault_engine.parse_values(store, [types.SimpleNamespace(name="TOKEN", value_zk=sealed)],
                                  allow_sealed=True)
    assert e.value.code == "not_a_person"


def test_a_test_key_never_endorses(w):
    _turn_on(w)
    _make_key(w)
    with pytest.raises(zk_endorse.ZkError) as e:
        zk_endorse.add(w["agent"], user=w["login"], items=[])
    assert e.value.code == "not_a_person"


# --- only its own conversations -----------------------------------------------------


def _browser_offer(user, priv):
    """A conversation key pair made in `user`'s browser, wrapped to their vault key."""
    import uuid

    conv = ec.generate_private_key(ec.SECP256R1())
    conversation = str(uuid.uuid4())
    scalar = conv.private_numbers().private_value.to_bytes(32, "big")
    jwk = ecdh_es.jwk_of(priv.public_key())
    return {"public_jwk": ecdh_es.jwk_of(conv.public_key()), "conversation": conversation,
            "wrap": ecdh_es.wrap(scalar, jwk, conversation_keys.wrap_info(conversation, user.pk)),
            "recipient_jkt": ecdh_es.jkt(jwk)}


def test_the_login_starts_a_sealed_conversation_and_opens_its_own_copy(w):
    _turn_on(w)
    _, priv = _make_key(w)
    assert conversation_keys.can_hold(user=w["login"])
    s = Session.objects.create(workspace=w["ws"], agent=w["agent"], created_by=w["login"],
                               metadata=dict(SENSITIVE))
    wrap = SessionKeyWrap.objects.get(session=s, user=w["login"])
    key = conversation_keys.key_of(s)
    scalar = unwrap(wrap.wrap, priv, conversation_keys.wrap_info(key.conversation, w["login"].pk))
    assert len(scalar) == 32
    # And a browser-made key, as the panel makes one, is accepted for it.
    assert conversation_keys.check_offer(w["login"], _browser_offer(w["login"], priv))["user_id"] == w["login"].pk


def test_a_test_key_receives_no_copy_of_a_conversation_it_is_not_in(w):
    _turn_on(w)
    _, priv = _make_key(w)
    hot = w["hot"]  # the owner's sensitive conversation; the login is not in it
    wrap = rewrap_for(hot, w["user"], w["key"], w["login"])
    with pytest.raises(conversation_keys.WrapRefused):
        conversation_keys.add_wrap(hot, w["login"], {k: v for k, v in wrap.items() if k != "recipient_jkt"},
                                   recipient_jkt=wrap["recipient_jkt"], by=w["user"])
    assert not SessionKeyWrap.objects.filter(session=hot, user=w["login"]).exists()
    # Once it is IN the conversation, a person in it may give it a copy.
    SessionParticipant.objects.create(session=hot, user=w["login"], role=SessionParticipant.EDITOR)
    conversation_keys.add_wrap(hot, w["login"], {k: v for k, v in wrap.items() if k != "recipient_jkt"},
                               recipient_jkt=wrap["recipient_jkt"], by=w["user"])
    assert SessionKeyWrap.objects.filter(session=hot, user=w["login"]).exists()


def test_adding_the_login_to_a_conversation_gives_it_a_copy_in_the_same_act(w):
    _turn_on(w)
    _make_key(w)
    hot = w["hot"]
    rec = w["c"].get(f"/api/canopy-sessions/{hot.pk}/participants/recipient",
                     {"email": w["login"].email}).json()
    assert rec["test_key"] is True
    wrap = rewrap_for(hot, w["user"], w["key"], w["login"])
    r = w["c"].post(f"/api/canopy-sessions/{hot.pk}/participants",
                    {"email": w["login"].email, "role": "editor", "wrap": wrap}, content_type="application/json")
    assert r.status_code == 200, r.content
    [row] = [p for p in r.json() if p["user_id"] == w["login"].pk]
    assert row["test_key"] is True and row["needs_key"] is False


def test_a_key_left_on_a_login_whose_switch_is_off_is_not_used(w):
    """Defence in depth: a keyset row that outlived its switch holds nothing new."""
    _turn_on(w)
    _make_key(w)
    w["agent"].test_vault_key = False
    w["agent"].save(update_fields=["test_vault_key"])
    assert not conversation_keys.can_hold(user=w["login"])
    assert conversation_keys.recipient(w["login"]) is None


# --- turning it off -----------------------------------------------------------------


def test_turning_it_off_deletes_the_key_and_every_copy_it_was_given(w):
    _turn_on(w)
    _make_key(w)
    s = Session.objects.create(workspace=w["ws"], agent=w["agent"], created_by=w["login"],
                               metadata=dict(SENSITIVE))
    assert SessionKeyWrap.objects.filter(session=s, user=w["login"]).exists()
    assert w["c"].get(_url(w)).json()["conversations"] == 1
    # Turning it off needs no passkey: a token of an admin can.
    owner_pat, _ = PersonalToken.create_for_user(user=w["user"], label="t")
    r = Client().put(_url(w), {"enabled": False}, content_type="application/json", **_bearer(owner_pat))
    assert r.status_code == 200, r.content
    assert r.json() == {**r.json(), "enabled": False, "has_key": False, "conversations": 0}
    assert not UserVaultKeyset.objects.filter(user=w["login"]).exists()
    assert not SessionKeyWrap.objects.filter(user=w["login"]).exists()
    # And with it off, the login can make none.
    assert _make_key(w)[0].status_code == 403


def test_the_people_list_says_which_key_is_an_agent_test_key(w):
    assert login_test_key.LABEL == "agent test key"
    _turn_on(w)
    assert login_test_key.logins_with_test_key([w["login"].pk, w["user"].pk]) == {w["login"].pk}
