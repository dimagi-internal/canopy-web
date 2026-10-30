"""Give an agent a secret (spec 2026-10-19-give-an-agent-a-secret-design.md).

The agent asks for a declared secret; a person's browser seals the value to the
agent's endorsed readers (`zks1`) and canopy-web stores ciphertext only. Every
seal and signature here is made with the NORMATIVE reference
(docs/vault/zk-v1/zk_v1_reference.py), so a round trip is the real protocol:
seal in "the browser", store, resolve on the device, open with its key.
"""
from __future__ import annotations

import json
import os

import pytest
from canopy_sdk.jose import make_dpop_proof
from cryptography.hazmat.primitives.asymmetric import ec
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import Client
from django.utils import timezone

from apps.agents.credentials import secret_requests, zk_endorse
from apps.agents.models import Agent, AgentCredential, AgentSecretRequest, AgentVaultEndorsement
from apps.canopy_sessions.models import Message, Session, SessionParticipant
from apps.harness.models import Runner, RunnerAssignment, RunnerDeviceKey
from apps.passkeys.models import Passkey, UserVaultKeyset
from apps.passkeys.testing import grant_step_up
from apps.tokens.models import PersonalToken
from apps.workspaces.models import Workspace, WorkspaceMembership
from tests.fixtures.vault_zk import VECTORS, ref

pytestmark = pytest.mark.django_db

BASE = "/api/agents/bailey"
RESOLVE = f"{BASE}/credentials/resolve"


@pytest.fixture(autouse=True)
def _fresh_cache():
    cache.clear()
    yield
    cache.clear()


def _pat(user) -> str:
    raw, _ = PersonalToken.create_for_user(user=user, label="t")
    return raw


def _bearer(token: str) -> dict:
    return {"HTTP_AUTHORIZATION": f"Bearer {token}"}


class F:
    pass


@pytest.fixture
def f(client):
    """bailey, born zero-knowledge (kv 0): its owner jj (with a vault key), one
    approved device, its own login, a chat with it that sam may write to and
    carol (a workspace member) may not."""
    User = get_user_model()
    x = F()
    x.jj = User.objects.create_user(username="jj", email="jj@dimagi.com")
    x.sam = User.objects.create_user(username="sam", email="sam@dimagi.com")
    x.carol = User.objects.create_user(username="carol", email="carol@dimagi.com")
    x.bot = User.objects.create_user(username="bailey-bot", email="bailey@dimagi-ai.com")
    x.ws = Workspace.objects.create(slug="family", display_name="Family", created_by=x.jj)
    WorkspaceMembership.objects.create(workspace=x.ws, user=x.jj, role=WorkspaceMembership.OWNER)
    for u in (x.sam, x.carol, x.bot):
        WorkspaceMembership.objects.create(workspace=x.ws, user=u, role=WorkspaceMembership.EDITOR)
    x.agent = Agent.objects.create(slug="bailey", name="Bailey", workspace=x.ws, owner=x.jj, user=x.bot,
                                   runtime_secrets=["GITHUB_TOKEN", "kindora"], zk_enabled=True, zk_kv=0)
    x.runner = Runner.objects.create(name="box", kind=Runner.CLOUD, owner=x.jj, status=Runner.ONLINE,
                                     workspace=x.ws, last_heartbeat_at=timezone.now(), capabilities={})
    RunnerAssignment.objects.create(agent=x.agent, runner=x.runner, rank=0)
    client.force_login(x.jj)
    grant_step_up(client)
    x.client = client
    x.jj_pat, x.sam_pat, x.carol_pat, x.bot_pat = (_pat(u) for u in (x.jj, x.sam, x.carol, x.bot))
    x.sign = ec.generate_private_key(ec.SECP256R1())
    x.ecdh = ec.generate_private_key(ec.SECP256R1())
    from canopy_sdk.keys import header_jwk

    res = Client().post("/api/runner-device-keys/", data={
        "runner": str(x.runner.pk), "public_jwk": header_jwk(x.sign), "kind": "secure-enclave", "label": "mbp",
        "ecdh_public_jwk": ref.jwk(x.ecdh.public_key())}, content_type="application/json", **_bearer(x.jj_pat))
    assert res.status_code in (200, 201), res.content
    x.device = RunnerDeviceKey.objects.get(pk=res.json()["id"])
    assert client.post(f"/api/runner-device-keys/{x.device.pk}/approve").status_code == 200
    x.device.refresh_from_db()
    pk = Passkey.objects.create(user=x.jj, credential_id=ref.b64u(b"cred-1"), public_key=b"x", prf_enabled=True)
    x.member = ec.generate_private_key(ec.SECP256R1())
    scalar = x.member.private_numbers().private_value.to_bytes(32, "big")
    UserVaultKeyset.objects.create(user=x.jj, public_jwk=ref.jwk(x.member.public_key()), seals=[
        ref.seal_user_key(scalar, str(x.jj.pk), pk.credential_id, os.urandom(32), os.urandom(16), os.urandom(12))])
    x.session = Session.objects.create(workspace=x.ws, agent=x.agent, created_by=x.jj, origin=Session.ORIGIN_WEB)
    SessionParticipant.objects.create(session=x.session, user=x.jj, role=SessionParticipant.OWNER)
    SessionParticipant.objects.create(session=x.session, user=x.sam, role=SessionParticipant.EDITOR)
    return x


def _ask(f, name="GITHUB_TOKEN", token=None, session=True):
    headers = _bearer(token or f.bot_pat)
    if session:
        headers["HTTP_X_CANOPY_PARENT_SESSION"] = str(f.session.pk)
    return Client().post(f"{BASE}/secret-requests", data={"name": name, "reason": "push to its repo"},
                         content_type="application/json", **headers)


def _readers(f):
    return [("device", f.device.ecdh_jkt, f.ecdh.public_key()), ("member", str(f.jj.pk), f.member.public_key())]


def _seal(f, plain="ghp_secret", name="GITHUB_TOKEN", kv=0, readers=None):
    readers = readers or _readers(f)
    return ref.seal_value(plain, str(f.agent.pk), name, kv, readers,
                          [ec.generate_private_key(ec.SECP256R1()) for _ in readers],
                          [os.urandom(12) for _ in readers])


def _fulfil(f, rid, value_zk, token=None, kv=0, client=None):
    body = {"kv": kv, "value_zk": value_zk}
    if client is not None:
        return client.put(f"{BASE}/secret-requests/{rid}/value", data=body, content_type="application/json")
    return Client().put(f"{BASE}/secret-requests/{rid}/value", data=body, content_type="application/json",
                        **_bearer(token or f.sam_pat))


# ---- §7 endorsements: the reference and the server agree ------------------------------

def test_the_server_verifies_the_reference_endorsement_vector():
    e, inp = VECTORS["outputs"]["endorsement"], VECTORS["inputs"]
    ok = zk_endorse.verify(inp["agent_id"], kind=e["kind"], target=e["target"], key_jkt=e["key_jkt"],
                           sig=e["sig"], endorser_jwk=inp["user_pub_jwk"])
    assert ok
    assert VECTORS["outputs"]["endorse_msg"] == zk_endorse.endorse_msg(
        inp["agent_id"], "device", inp["device_jkt"], inp["device_jkt"])
    for bad in ({"key_jkt": inp["device_jkt"][::-1]}, {"target": "other"}, {"kind": "member"}):
        args = {**{k: e[k] for k in ("kind", "target", "key_jkt")}, **bad}
        assert not zk_endorse.verify(inp["agent_id"], sig=e["sig"], endorser_jwk=inp["user_pub_jwk"], **args)
    assert not zk_endorse.verify("43", kind=e["kind"], target=e["target"], key_jkt=e["key_jkt"], sig=e["sig"],
                                 endorser_jwk=inp["user_pub_jwk"])
    assert not zk_endorse.verify(inp["agent_id"], kind=e["kind"], target=e["target"], key_jkt=e["key_jkt"],
                                 sig=e["sig"], endorser_jwk=inp["device_pub_jwk"])


def _endorse_device(f, signer=None, by_jkt=None):
    e = ref.endorse(str(f.agent.pk), "device", f.device.ecdh_jkt, f.ecdh.public_key(), signer or f.member,
                    str(f.jj.pk))
    return f.client.post(f"{BASE}/vault/zk/endorsements", data={"endorsements": [{
        "kind": e["kind"], "target": e["target"], "key_jkt": e["key_jkt"],
        "by_jkt": by_jkt or e["by_jkt"], "sig": e["sig"]}]}, content_type="application/json")


def test_an_admin_endorses_a_reader_with_their_own_key_and_it_is_served(f):
    res = _endorse_device(f)
    assert res.status_code == 200, res.content
    assert res.json() == {"stored": 1}
    got = f.client.get(f"{BASE}/vault/zk/recipients").json()
    assert {(r["kind"], r["target"]) for r in got["recipients"]} == {
        ("device", f.device.ecdh_jkt), ("member", str(f.jj.pk))}
    [e] = got["endorsements"]
    assert (e["kind"], e["target"], e["by"], e["by_email"]) == ("device", f.device.ecdh_jkt, str(f.jj.pk),
                                                                "jj@dimagi.com")
    assert e["by_public_jwk"] == ref.jwk(f.member.public_key())
    assert got["declared"] == ["GITHUB_TOKEN", "kindora"] and got["stored"] == [] and got["kv"] == 0


def test_an_endorsement_is_refused_unless_it_is_the_callers_own_valid_signature(f):
    stranger = ec.generate_private_key(ec.SECP256R1())
    res = _endorse_device(f, signer=stranger)  # by_jkt names the stranger's key
    assert res.status_code == 422 and res.json()["error"] == "zk_not_your_key"
    res = _endorse_device(f, signer=stranger, by_jkt=ref.jkt(f.member.public_key()))  # claims to be mine
    assert res.status_code == 422 and res.json()["error"] == "zk_bad_signature"
    other = ec.generate_private_key(ec.SECP256R1())
    e = ref.endorse(str(f.agent.pk), "device", ref.jkt(other.public_key()), other.public_key(), f.member,
                    str(f.jj.pk))
    res = f.client.post(f"{BASE}/vault/zk/endorsements", data={"endorsements": [
        {k: e[k] for k in ("kind", "target", "key_jkt", "by_jkt", "sig")}]}, content_type="application/json")
    assert res.status_code == 422 and res.json()["error"] == "zk_invalid_target"
    assert not AgentVaultEndorsement.objects.exists()


def test_only_an_admin_may_endorse(f):
    e = ref.endorse(str(f.agent.pk), "device", f.device.ecdh_jkt, f.ecdh.public_key(), f.member, str(f.jj.pk))
    res = Client().post(f"{BASE}/vault/zk/endorsements", data={"endorsements": [
        {k: e[k] for k in ("kind", "target", "key_jkt", "by_jkt", "sig")}]}, content_type="application/json",
        **_bearer(f.sam_pat))
    assert res.status_code in (403, 404)


# ---- asking -----------------------------------------------------------------------------

def test_the_agent_asks_for_a_declared_secret_from_its_chat(f):
    res = _ask(f)
    assert res.status_code == 200, res.content
    body = res.json()
    assert body["name"] == "GITHUB_TOKEN" and body["status"] == "open" and body["has_value"] is False
    assert body["secret_request_id"] == body["id"] and body["session_id"] == str(f.session.pk)
    assert "never reaches you" in body["message"]
    assert "value" not in body and "value_zk" not in body
    again = _ask(f).json()
    assert again["id"] == body["id"], "a second ask from the same chat returns the open request"


def test_asking_is_declared_names_only_and_agent_or_admin_only(f):
    res = _ask(f, name="AWS_ROOT")
    assert res.status_code == 422 and res.json()["error"] == "not_declared"
    assert "GITHUB_TOKEN" in res.json()["detail"]
    res = _ask(f, token=f.sam_pat)
    assert res.status_code == 403
    assert _ask(f, token=f.jj_pat).status_code == 200


def test_a_session_header_naming_another_agents_chat_is_ignored(f):
    other = Agent.objects.create(slug="other", name="Other", workspace=f.ws, owner=f.jj)
    foreign = Session.objects.create(workspace=f.ws, agent=other, created_by=f.jj, origin=Session.ORIGIN_WEB)
    res = Client().post(f"{BASE}/secret-requests", data={"name": "GITHUB_TOKEN"}, content_type="application/json",
                        HTTP_X_CANOPY_PARENT_SESSION=str(foreign.pk), **_bearer(f.bot_pat))
    assert res.status_code == 200 and res.json()["session_id"] is None


def test_a_contacts_chat_is_never_asked_for_a_secret(f):
    # A contact can't fill a secret and never sees a seal card, so the agent
    # isn't given the option: it is told to ask its owner instead.
    from apps.contacts.models import Contact

    contact = Contact.objects.create(workspace=f.ws, email="visitor@example.org")
    f.session.contact = contact
    f.session.save(update_fields=["contact"])
    res = _ask(f)
    assert res.status_code == 409 and res.json()["error"] == "contact_chat"
    assert "jj@dimagi.com" in res.json()["detail"] and "Security tab" in res.json()["detail"]
    assert not AgentSecretRequest.objects.exists()


def test_the_contact_door_never_carries_a_seal_card():
    # The contact transcript route doesn't annotate `secret_request`, on purpose.
    from pathlib import Path

    src = Path(__file__).resolve().parents[1].joinpath("apps/contacts/contact_api.py").read_text()
    assert "secret_request" not in src


# ---- recipients ---------------------------------------------------------------------------

def test_the_credentials_page_lists_open_requests_including_one_from_no_chat(f):
    in_chat = _ask(f).json()["id"]
    no_chat = _ask(f, name="kindora", session=False).json()
    assert no_chat["session_id"] is None
    listed = Client().get(f"{BASE}/secret-requests", **_bearer(f.jj_pat))
    assert listed.status_code == 200, listed.content
    assert [r["id"] for r in listed.json()] == [no_chat["id"], in_chat]
    assert all("value" not in r and "value_zk" not in r for r in listed.json())
    assert _fulfil(f, in_chat, _seal(f), token=f.jj_pat).status_code == 200
    AgentSecretRequest.objects.filter(pk=no_chat["id"]).update(expires_at=timezone.now())
    assert Client().get(f"{BASE}/secret-requests", **_bearer(f.jj_pat)).json() == []


def test_only_an_admin_lists_the_open_requests(f):
    _ask(f)
    for token in (f.sam_pat, f.bot_pat):
        assert Client().get(f"{BASE}/secret-requests", **_bearer(token)).status_code == 403


def test_recipients_are_for_admins_and_whoever_may_answer_the_open_request(f):
    rid = _ask(f).json()["id"]
    url = f"{BASE}/vault/zk/recipients"
    assert Client().get(url, **_bearer(f.sam_pat)).status_code == 403
    assert Client().get(f"{url}?secret_request={rid}", **_bearer(f.sam_pat)).status_code == 200
    assert Client().get(f"{url}?secret_request={rid}", **_bearer(f.carol_pat)).status_code == 403
    assert Client().get(f"{url}?secret_request={rid}", **_bearer(f.bot_pat)).status_code == 403


# ---- answering ----------------------------------------------------------------------------

def _resolve(f):
    htu = f"http://testserver{RESOLVE}"
    return Client().get(RESOLVE, HTTP_DPOP=make_dpop_proof(f.sign, "GET", htu), HTTP_X_CANOPY_TURN_ID="t-1",
                        **_bearer(f.jj_pat))


def test_a_chat_writer_fills_a_missing_secret_and_the_runner_opens_it(f):
    rid = _ask(f).json()["id"]
    res = _fulfil(f, rid, _seal(f, "ghp_from_sam"))
    assert res.status_code == 200, res.content
    assert res.json()["status"] == "fulfilled" and res.json()["fulfilled_by_email"] == "sam@dimagi.com"
    stored = AgentCredential.objects.get(agent=f.agent, name="GITHUB_TOKEN")
    assert stored.value_zk.startswith("zks1:")
    assert "ghp_from_sam" not in stored.value_zk
    # The runner resolves the ciphertext and opens its own entry.
    got = _resolve(f)
    assert got.status_code == 200, got.content
    sealed = got.json()["values"]["GITHUB_TOKEN"]
    assert ref.open_sealed_value(sealed, str(f.agent.pk), "GITHUB_TOKEN", "device", f.device.ecdh_jkt,
                                 f.ecdh) == "ghp_from_sam"
    # The agent learns the outcome, never the value.
    status = Client().get(f"{BASE}/secret-requests/{rid}", **_bearer(f.bot_pat)).json()
    assert status["status"] == "fulfilled" and status["has_value"] is True
    assert "ghp_from_sam" not in json.dumps(status)


def test_only_sealed_values_are_accepted(f):
    rid = _ask(f).json()["id"]
    for bad in ("ghp_plaintext", "zk1:" + ref.b64u(os.urandom(40))):
        res = _fulfil(f, rid, bad)
        assert res.status_code == 422 and res.json()["error"] == "sealed_only", bad
    assert not AgentCredential.objects.filter(agent=f.agent).exists()


def test_a_non_admin_cannot_replace_but_an_admin_can(f):
    rid = _ask(f).json()["id"]
    assert _fulfil(f, rid, _seal(f, "first")).status_code == 200
    rid2 = _ask(f).json()["id"]
    assert rid2 != rid
    res = _fulfil(f, rid2, _seal(f, "attacker"))
    assert res.status_code == 409 and res.json()["error"] == "value_exists"
    assert _fulfil(f, rid2, _seal(f, "rotated"), token=f.jj_pat).status_code == 200


def test_no_agent_login_may_answer_not_even_its_own_request(f):
    rid = _ask(f).json()["id"]
    assert _fulfil(f, rid, _seal(f), token=f.bot_pat).status_code == 403
    assert _fulfil(f, rid, _seal(f), token=f.carol_pat).status_code == 403
    assert AgentSecretRequest.objects.get(pk=rid).status == "open"


def test_an_answered_or_expired_request_is_closed(f):
    rid = _ask(f).json()["id"]
    assert _fulfil(f, rid, _seal(f), token=f.jj_pat).status_code == 200
    res = _fulfil(f, rid, _seal(f), token=f.jj_pat)
    assert res.status_code == 409 and res.json()["error"] == "request_closed"
    rid = _ask(f, name="kindora").json()["id"]
    AgentSecretRequest.objects.filter(pk=rid).update(expires_at=timezone.now())
    res = _fulfil(f, rid, _seal(f, name="kindora"), token=f.jj_pat)
    assert res.status_code == 409 and res.json()["error"] == "request_closed"


def test_the_kv_must_be_current(f):
    rid = _ask(f).json()["id"]
    res = _fulfil(f, rid, _seal(f), kv=3)
    assert res.status_code == 409 and res.json()["error"] == "zk_kv_mismatch"


def test_a_browser_answer_needs_a_passkey_step_up(f):
    rid = _ask(f).json()["id"]
    sam = Client()
    sam.force_login(f.sam)
    res = _fulfil(f, rid, _seal(f), client=sam)
    assert res.status_code == 403 and "step_up" in res.content.decode()
    grant_step_up(sam)
    assert _fulfil(f, rid, _seal(f), client=sam).status_code == 200


# ---- the chat row ---------------------------------------------------------------------------

def test_the_tool_result_that_asked_carries_the_card_in_its_own_chat_only(f):
    body = _ask(f).json()
    text = json.dumps(body)
    row = Message(session=f.session, turn_index=1, role=Message.TOOL_RESULT, plaintext=text,
                  content={"tool_use_id": "toolu_1"})
    other = Session.objects.create(workspace=f.ws, agent=f.agent, created_by=f.jj, origin=Session.ORIGIN_WEB)
    stray = Message(session=other, turn_index=1, role=Message.TOOL_RESULT, plaintext=text,
                    content={"tool_use_id": "toolu_2"})
    secret_requests.annotate(f.session, [row])
    secret_requests.annotate(other, [stray])
    assert row.secret_request == {"id": body["id"], "agent": "bailey", "name": "GITHUB_TOKEN"}
    assert not getattr(stray, "secret_request", None)
    # The live frame: an MCP tool_result block whose text is the JSON, escaped.
    block = {"tool_use_id": "toolu_1", "content": [{"type": "text", "text": json.dumps(text)}]}
    assert secret_requests.for_result_block(f.session, block) == row.secret_request
    assert secret_requests.for_result_block(f.session, {**block, "is_error": True}) is None
