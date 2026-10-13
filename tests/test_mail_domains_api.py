# tests/test_mail_domains_api.py
from __future__ import annotations

import datetime as dt

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.utils import timezone

from apps.agents.models import Agent
from apps.mail import domains, ses
from apps.mail.models import MailDomain, MailRoute
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def stub_aws(monkeypatch, settings):
    settings.MAIL_ENABLED = True
    monkeypatch.setattr(ses, "create_identity", lambda d, mail_from: ["t1", "t2", "t3"])
    monkeypatch.setattr(ses, "identity_verified", lambda d: True)
    monkeypatch.setattr(ses, "delete_identity", lambda d: None)
    from apps.mail import dns
    monkeypatch.setattr(dns, "_resolve", lambda n, t: ["10 mx.zoho.com."] if t == "MX" else [])


@pytest.fixture(autouse=True)
def _fresh_cache():
    from django.core.cache import cache
    cache.clear()
    yield
    cache.clear()


def _client_error(code="AccessDenied"):
    from botocore.exceptions import ClientError
    return ClientError({"Error": {"Code": code, "Message": "no"}}, "Op")


@pytest.fixture()
def owner():
    return User.objects.create_user("jj", "jjackson@dimagi.com", "pw")


@pytest.fixture()
def ws(owner):
    w = Workspace.objects.create(slug="marshellis", display_name="M", created_by=owner)
    WorkspaceMembership.objects.create(workspace=w, user=owner, role="owner")
    return w


def _c(u):
    c = Client(); c.force_login(u); return c


def test_admin_adds_a_domain_and_sees_records_and_current_provider(owner, ws):
    r = _c(owner).post("/api/mail/domains/marshellis", {"name": "Marshellis.com"},
                       content_type="application/json")
    assert r.status_code == 200, r.content
    body = r.json()
    assert body["name"] == "marshellis.com"
    assert body["mx_provider"] == "Zoho"
    assert {rec["purpose"] for rec in body["records"]} >= {"dkim-1", "spf", "dmarc", "mx"}


def test_editor_cannot_add_a_domain(ws):
    ed = User.objects.create_user("ed", "ed@x.com", "pw")
    WorkspaceMembership.objects.create(workspace=ws, user=ed, role="editor")
    r = _c(ed).post("/api/mail/domains/marshellis", {"name": "marshellis.com"},
                    content_type="application/json")
    assert r.status_code == 403


def test_domain_taken_elsewhere_is_409_without_naming_the_holder(owner, ws):
    other = Workspace.objects.create(slug="acme-holdco", display_name="Acme Holdco", created_by=owner)
    MailDomain.objects.create(workspace=other, name="marshellis.com")
    r = _c(owner).post("/api/mail/domains/marshellis", {"name": "marshellis.com"},
                       content_type="application/json")
    assert r.status_code == 409
    assert r.json()["detail"] == "that domain is already registered"
    assert "acme" not in r.content.decode().lower()


def test_forward_route_requires_a_member(owner, ws):
    d = MailDomain.objects.create(workspace=ws, name="marshellis.com", status="verified")
    stranger = User.objects.create_user("s", "s@x.com", "pw")
    r = _c(owner).put(f"/api/mail/domains/marshellis/{d.pk}/routes",
                      {"local_part": "jjackson", "kind": "forward", "forward_to_user_id": stranger.pk},
                      content_type="application/json")
    assert r.status_code == 422
    ok = _c(owner).put(f"/api/mail/domains/marshellis/{d.pk}/routes",
                       {"local_part": "jjackson", "kind": "forward", "forward_to_user_id": owner.pk},
                       content_type="application/json")
    assert ok.status_code == 200, ok.content


def test_reserved_local_part_cannot_be_routed(owner, ws):
    d = MailDomain.objects.create(workspace=ws, name="marshellis.com", status="verified")
    r = _c(owner).put(f"/api/mail/domains/marshellis/{d.pk}/routes",
                      {"local_part": "canopy-forwarder", "kind": "drop"}, content_type="application/json")
    assert r.status_code == 422


def test_resolve_exact_beats_catch_all_and_subdomain_uses_parent(owner, ws):
    d = MailDomain.objects.create(workspace=ws, name="marshellis.com", status="verified")
    a = Agent.objects.create(slug="mada", name="Ada", workspace=ws)
    MailRoute.objects.create(domain=d, local_part="ada", kind="agent", agent=a)
    MailRoute.objects.create(domain=d, local_part="*", kind="forward", forward_to=owner)
    assert domains.resolve("Ada@marshellis.com")[1].kind == "agent"
    assert domains.resolve("apple@marshellis.com")[1].kind == "forward"
    assert domains.resolve("ada@mail.marshellis.com")[1].kind == "agent"
    assert domains.resolve("x@nobody.org") == (None, None)


def test_pending_domain_routes_nothing(owner, ws):
    d = MailDomain.objects.create(workspace=ws, name="marshellis.com", status="pending")
    MailRoute.objects.create(domain=d, local_part="*", kind="forward", forward_to=owner)
    assert domains.resolve("a@marshellis.com") == (d, None)


def test_refresh_flips_pending_to_verified(owner, ws):
    d = MailDomain.objects.create(workspace=ws, name="marshellis.com", dkim_tokens=["t1"])
    assert domains.refresh(d, force=True).status == "verified"


def _post(owner, name="marshellis.com"):
    return _c(owner).post("/api/mail/domains/marshellis", {"name": name}, content_type="application/json")


def test_add_when_ses_refuses_is_502_and_deletes_nothing_we_did_not_create(owner, ws, monkeypatch):
    deleted = []
    monkeypatch.setattr(ses, "create_identity", lambda d, mail_from: (_ for _ in ()).throw(_client_error()))
    monkeypatch.setattr(ses, "delete_identity", lambda d: deleted.append(d))
    r = _post(owner)
    assert r.status_code == 502 and "AccessDenied" in r.json()["detail"]
    assert not MailDomain.objects.exists() and deleted == []


def test_failure_after_a_successful_create_deletes_the_identity(owner, ws, monkeypatch):
    deleted = []
    monkeypatch.setattr(domains, "refresh",
                        lambda d, force=False: (_ for _ in ()).throw(_client_error("Throttling")))
    monkeypatch.setattr(ses, "delete_identity", lambda d: deleted.append(d))
    assert _post(owner).status_code == 502
    assert not MailDomain.objects.exists() and deleted == ["marshellis.com"]


def test_existing_ses_identity_is_409_row_gone_and_never_deleted(owner, ws, monkeypatch):
    deleted = []

    def exists(d, mail_from):
        raise ses.IdentityExists(d)
    monkeypatch.setattr(ses, "create_identity", exists)
    monkeypatch.setattr(ses, "delete_identity", lambda d: deleted.append(d))
    r = _post(owner)
    assert r.status_code == 409 and "mail provider" in r.json()["detail"]
    assert not MailDomain.objects.exists() and deleted == []


@pytest.mark.parametrize("name", ["canopy.dimagi.com", "dimagi.com", "x.amazonaws.com"])
def test_reserved_domains_are_refused_before_any_write_or_ses_call(owner, ws, monkeypatch, name):
    calls = []
    monkeypatch.setattr(ses, "create_identity", lambda d, mail_from: calls.append(d))
    r = _post(owner, name)
    assert r.status_code == 422 and r.json()["detail"] == "that domain is reserved"
    assert calls == [] and not MailDomain.objects.exists()


def test_add_cleans_up_on_unexpected_error_even_if_delete_fails(owner, ws, monkeypatch):
    def boom(d):
        raise _client_error("Throttling")
    monkeypatch.setattr(domains, "refresh", lambda d, force=False: (_ for _ in ()).throw(RuntimeError("x")))
    monkeypatch.setattr(ses, "delete_identity", boom)
    with pytest.raises(RuntimeError):
        domains.add_domain(ws, "marshellis.com", by=owner)
    assert not MailDomain.objects.exists()


def test_add_when_mail_is_off_is_503_and_leaves_nothing(owner, ws, monkeypatch):
    def off(d, mail_from):
        raise ses.MailOff("off")
    monkeypatch.setattr(ses, "create_identity", off)
    monkeypatch.setattr(ses, "delete_identity", lambda d: (_ for _ in ()).throw(ses.MailOff("off")))
    assert _post(owner).status_code == 503
    assert not MailDomain.objects.exists()


def test_remove_survives_ses_not_found_and_other_errors(owner, ws, monkeypatch):
    for code in ("NotFoundException", "Throttling"):
        d = MailDomain.objects.create(workspace=ws, name=f"{code.lower()}.com", status="verified")
        monkeypatch.setattr(ses, "delete_identity", lambda n, c=code: (_ for _ in ()).throw(_client_error(c)))
        r = _c(owner).delete(f"/api/mail/domains/marshellis/{d.pk}")
        assert r.status_code == 204
        assert not MailDomain.objects.filter(pk=d.pk).exists()


def test_dns_lookups_are_cached_for_a_minute(owner, ws, monkeypatch):
    from apps.mail import dns
    d = MailDomain.objects.create(workspace=ws, name="marshellis.com", status="verified",
                                  dkim_tokens=["t1"], checked_at=timezone.now())
    calls = []
    monkeypatch.setattr(dns, "_resolve", lambda n, t: calls.append((n, t)) or [])
    _c(owner).get("/api/mail/domains/marshellis")
    n = len(calls)
    assert n > 0
    _c(owner).get("/api/mail/domains/marshellis")
    assert len(calls) == n
    MailDomain.objects.filter(pk=d.pk).update(checked_at=timezone.now() - dt.timedelta(minutes=5))
    domains.refresh(MailDomain.objects.get(pk=d.pk), force=True)  # a forced refresh drops the cache
    _c(owner).get("/api/mail/domains/marshellis")
    assert len(calls) > n


def test_forced_refresh_has_a_ten_second_cooldown(ws, monkeypatch):
    from apps.mail import dns
    d = MailDomain.objects.create(workspace=ws, name="marshellis.com")
    calls = []
    monkeypatch.setattr(dns, "_resolve", lambda n, t: calls.append(n) or [])
    domains.refresh(d, force=True)
    domains.refresh(d, force=True)
    assert len(calls) == 1


def test_refresh_does_not_clobber_a_concurrent_suspension(ws):
    d = MailDomain.objects.create(workspace=ws, name="marshellis.com")
    MailDomain.objects.filter(pk=d.pk).update(status="suspended")
    domains.refresh(d, force=True)
    assert MailDomain.objects.get(pk=d.pk).status == "suspended"


def test_pending_subdomain_does_not_shadow_live_parent(owner, ws):
    live = MailDomain.objects.create(workspace=ws, name="marshellis.com", status="verified")
    MailDomain.objects.create(workspace=ws, name="mail.marshellis.com", status="pending")
    MailRoute.objects.create(domain=live, local_part="*", kind="forward", forward_to=owner)
    dom, route = domains.resolve("a@mail.marshellis.com")
    assert dom == live and route is not None


def test_forwarder_address_never_resolves(owner, ws):
    d = MailDomain.objects.create(workspace=ws, name="marshellis.com", status="verified")
    MailRoute.objects.create(domain=d, local_part="*", kind="forward", forward_to=owner)
    assert domains.resolve("canopy-forwarder@marshellis.com") == (d, None)


def test_local_part_length_and_punycode_tld(owner, ws):
    d = MailDomain.objects.create(workspace=ws, name="marshellis.com", status="verified")
    with pytest.raises(domains.DomainError):
        domains.save_route(d, local_part="a" * 65, kind="drop")
    assert domains.add_domain(ws, "example.xn--p1ai", by=owner).name == "example.xn--p1ai"


def test_mail_refresh_command_continues_after_a_failure(ws, monkeypatch, capsys):
    from django.core.management import call_command
    MailDomain.objects.create(workspace=ws, name="a.com")
    MailDomain.objects.create(workspace=ws, name="b.com")
    seen = []

    def flaky(d, force=False):
        seen.append(d.name)
        if d.name == "a.com":
            raise RuntimeError("boom")
        return d
    monkeypatch.setattr(domains, "refresh", flaky)
    call_command("mail_refresh")
    assert sorted(seen) == ["a.com", "b.com"]


def _mail_world(ws, owner):
    from apps.mail.models import MailMessage, MailThread
    d = MailDomain.objects.create(workspace=ws, name="marshellis.com", status="verified")
    a = Agent.objects.create(slug="mada", name="Ada", workspace=ws, owner=owner)
    r = MailRoute.objects.create(domain=d, local_part="ada", kind="agent", agent=a)
    t = MailThread.objects.create(workspace=ws, route=r, subject="Hello", message_count=1)
    MailMessage.objects.create(thread=t, direction="in", message_id="<in-1@x.org>",
                               from_addr="x@x.org", subject="Hello", text="hi")
    return d, r, t


def test_deleting_a_route_keeps_its_threads_and_messages(owner, ws):
    from apps.mail.models import MailMessage, MailThread
    d, r, t = _mail_world(ws, owner)
    assert t.address == "ada@marshellis.com"
    resp = _c(owner).delete(f"/api/mail/domains/marshellis/{d.pk}/routes/{r.pk}")
    assert resp.status_code == 204
    t = MailThread.objects.get(pk=t.pk)
    assert t.route_id is None and t.address == "ada@marshellis.com"
    assert MailMessage.objects.filter(thread=t).count() == 1


def test_removing_a_domain_keeps_its_threads(owner, ws):
    from apps.mail.models import MailMessage, MailThread
    d, r, t = _mail_world(ws, owner)
    assert _c(owner).delete(f"/api/mail/domains/marshellis/{d.pk}").status_code == 204
    assert not MailDomain.objects.exists() and not MailRoute.objects.exists()
    assert MailThread.objects.get(pk=t.pk).route_id is None
    assert MailMessage.objects.count() == 1


def test_threads_without_a_route_still_list_read_and_refuse_replies(owner, ws):
    d, r, t = _mail_world(ws, owner)
    r.delete()
    c = _c(owner)
    items = c.get("/api/mail/threads/marshellis").json()["items"]
    assert [(i["address"], i["agent"]) for i in items] == [("ada@marshellis.com", "")]
    got = c.get(f"/api/mail/threads/marshellis/{t.uuid}")
    assert got.status_code == 200 and got.json()["address"] == "ada@marshellis.com"
    assert len(got.json()["messages"]) == 1
    rep = c.post(f"/api/mail/threads/marshellis/{t.uuid}/reply", {"text": "x"},
                 content_type="application/json")
    assert rep.status_code == 409 and rep.json()["detail"] == "this address no longer exists"
    st = c.post(f"/api/mail/threads/marshellis/{t.uuid}/state", {"unread": False},
                content_type="application/json")
    assert st.status_code == 409


def test_reply_on_a_disabled_route_is_409(owner, ws):
    d, r, t = _mail_world(ws, owner)
    MailRoute.objects.filter(pk=r.pk).update(enabled=False)
    rep = _c(owner).post(f"/api/mail/threads/marshellis/{t.uuid}/reply", {"text": "x"},
                         content_type="application/json")
    assert rep.status_code == 409 and rep.json()["detail"] == "this address no longer exists"


def test_catch_all_cannot_belong_to_an_agent(owner, ws):
    d = MailDomain.objects.create(workspace=ws, name="marshellis.com", status="verified")
    Agent.objects.create(slug="mada", name="Ada", workspace=ws, owner=owner)
    r = _c(owner).put(f"/api/mail/domains/marshellis/{d.pk}/routes",
                      {"local_part": "*", "kind": "agent", "agent_slug": "mada"},
                      content_type="application/json")
    assert r.status_code == 422
    assert r.json()["detail"] == "the catch-all can forward or drop, not belong to an agent"
    assert not MailRoute.objects.exists()


def test_agent_address_and_send_never_pick_the_catch_all_and_are_deterministic(owner, ws, monkeypatch):
    sent = []
    monkeypatch.setattr(ses, "send_raw", lambda **kw: sent.append(kw) or "ses-1")
    d = MailDomain.objects.create(workspace=ws, name="marshellis.com", status="verified")
    a = Agent.objects.create(slug="mada", name="Ada", workspace=ws, owner=owner)
    # a legacy catch-all agent row (save_route now refuses one) and two real addresses
    MailRoute.objects.create(domain=d, local_part="*", kind="agent", agent=a)
    MailRoute.objects.create(domain=d, local_part="zed", kind="agent", agent=a)
    MailRoute.objects.create(domain=d, local_part="ada", kind="agent", agent=a)
    c = _c(owner)
    for _ in range(3):
        got = c.get("/api/mail/threads/marshellis", {"agent": "mada"}).json()
        assert got["agent_address"] == "ada@marshellis.com"
    r = c.post("/api/mail/send/marshellis", {"agent": "mada", "to": ["x@x.org"], "subject": "s", "text": "t"},
               content_type="application/json")
    assert r.status_code == 200, r.content
    assert sent[0]["from_addr"] == "ada@marshellis.com"


def test_send_with_only_a_catch_all_agent_route_is_404(owner, ws):
    d = MailDomain.objects.create(workspace=ws, name="marshellis.com", status="verified")
    a = Agent.objects.create(slug="mada", name="Ada", workspace=ws, owner=owner)
    MailRoute.objects.create(domain=d, local_part="*", kind="agent", agent=a)
    c = _c(owner)
    assert c.get("/api/mail/threads/marshellis", {"agent": "mada"}).json()["agent_address"] == ""
    r = c.post("/api/mail/send/marshellis", {"agent": "mada", "to": ["x@x.org"], "subject": "s", "text": "t"},
               content_type="application/json")
    assert r.status_code == 404


def test_patching_the_cap_does_not_undo_a_concurrent_suspension(owner, ws, monkeypatch):
    from apps.mail import api as mail_api
    d = MailDomain.objects.create(workspace=ws, name="marshellis.com", status="verified")
    real = mail_api._domain

    def stale(w, pk):  # the row is read, then auto-suspension lands before the save
        got = real(w, pk)
        MailDomain.objects.filter(pk=pk).update(status="suspended", suspended_reason="bounces")
        return got

    monkeypatch.setattr(mail_api, "_domain", stale)
    r = _c(owner).patch(f"/api/mail/domains/marshellis/{d.pk}", {"daily_send_cap": 300},
                        content_type="application/json")
    assert r.status_code == 200, r.content
    d.refresh_from_db()
    assert d.daily_send_cap == 300 and d.status == "suspended" and d.suspended_reason == "bounces"
    assert r.json()["status"] == "suspended"


def test_deleting_an_agents_route_keeps_its_threads_in_the_agents_list(owner, ws):
    d, r, t = _mail_world(ws, owner)
    assert t.agent_slug == "mada"
    assert _c(owner).delete(f"/api/mail/domains/marshellis/{d.pk}/routes/{r.pk}").status_code == 204
    got = _c(owner).get("/api/mail/threads/marshellis", {"agent": "mada"}).json()
    assert [i["thread_id"] for i in got["items"]] == [str(t.uuid)]
    assert got["agent_address"] == ""
    assert _c(owner).get("/api/mail/threads/marshellis", {"agent": "other"}).json()["items"] == []


def _held_elsewhere(owner, *, status="pending", age_days=8):
    # Another tenant the adder is no member of.
    them = User.objects.create_user("acme-owner", "boss@acme-holdco.example", "pw")
    other = Workspace.objects.create(slug="acme-holdco", display_name="Acme Holdco", created_by=them)
    WorkspaceMembership.objects.create(workspace=other, user=them, role="owner")
    d = MailDomain.objects.create(workspace=other, name="marshellis.com", status=status)
    MailDomain.objects.filter(pk=d.pk).update(created_at=timezone.now() - dt.timedelta(days=age_days))
    return d


@pytest.fixture()
def unverified(monkeypatch):
    """SES says the stale holder never verified (NotFound, as for a deleted identity);
    the newly added domain then reads as not verified yet."""
    calls = []

    def nope(d):
        calls.append(d)
        if len(calls) == 1:
            raise _client_error("NotFoundException")
        return False
    monkeypatch.setattr(ses, "identity_verified", nope)


def test_a_stale_pending_domain_elsewhere_is_released_then_added(owner, ws, monkeypatch, settings, caplog,
                                                                  unverified):
    from apps.events.models import SettingChange
    from apps.mail.models import MailMessage, MailThread
    settings.MAIL_PENDING_DOMAIN_TTL_DAYS = 7
    stale = _held_elsewhere(owner)
    route = MailRoute.objects.create(domain=stale, local_part="x", kind="drop")
    t = MailThread.objects.create(workspace=stale.workspace, route=route, subject="kept")
    MailMessage.objects.create(thread=t, direction="in", message_id="<k@x>")
    deleted = []
    monkeypatch.setattr(ses, "delete_identity", lambda d: deleted.append(d))
    with caplog.at_level("INFO", logger="apps.mail.domains"):
        r = _post(owner)
    assert r.status_code == 200, r.content
    assert deleted == ["marshellis.com"]
    assert MailDomain.objects.get(name="marshellis.com").workspace == ws
    assert not MailRoute.objects.filter(pk=route.pk).exists()
    t.refresh_from_db()
    assert t.route is None and t.messages.count() == 1
    assert any("released stale pending domain marshellis.com (held by workspace acme-holdco" in rec.getMessage()
               for rec in caplog.records)
    # One audit row, the adder's own; the holder's workspace gets none from this request.
    assert not SettingChange.objects.filter(workspace=stale.workspace).exists()
    added = SettingChange.objects.get()
    assert added.workspace == ws
    assert added.summary == "added marshellis.com; released a stale unverified claim on it"
    assert added.changes == [{"scope": "mail.domain:marshellis.com",
                              "before": {"name": "marshellis.com", "released_stale_claim": True},
                              "after": {"name": "marshellis.com"}}]
    # What the adder can read back names nothing of the other tenant.
    body = _c(owner).get("/api/events/setting-changes").content.decode().lower()
    assert "marshellis.com" in body
    assert "acme" not in body


def test_a_stale_row_that_ses_says_verified_is_flipped_not_released(owner, ws, monkeypatch):
    held = _held_elsewhere(owner)
    monkeypatch.setattr(ses, "identity_verified", lambda d: True)
    monkeypatch.setattr(ses, "delete_identity", lambda d: pytest.fail("deleted a verified identity"))
    r = _post(owner)
    assert r.status_code == 409 and r.json()["detail"] == "that domain is already registered"
    held.refresh_from_db()
    assert held.status == "verified" and held.workspace.slug == "acme-holdco"
    assert MailDomain.objects.count() == 1


@pytest.mark.parametrize("fails_on", ["identity_verified", "delete_identity"])
def test_a_provider_failure_keeps_the_stale_row_and_is_502(owner, ws, monkeypatch, unverified, fails_on):
    stale = _held_elsewhere(owner)
    monkeypatch.setattr(ses, fails_on, lambda d: (_ for _ in ()).throw(_client_error("Throttling")))
    r = _post(owner)
    assert r.status_code == 502 and "the mail provider refused: Throttling" in r.json()["detail"]
    assert MailDomain.objects.get(name="marshellis.com").pk == stale.pk


def test_an_identity_already_gone_is_fine_on_release(owner, ws, monkeypatch, unverified):
    _held_elsewhere(owner)
    monkeypatch.setattr(ses, "delete_identity", lambda d: (_ for _ in ()).throw(_client_error("NotFoundException")))
    assert _post(owner).status_code == 200
    assert MailDomain.objects.get(name="marshellis.com").workspace == ws


def test_a_stale_pending_domain_is_kept_when_mail_is_off(owner, ws, monkeypatch, unverified):
    stale = _held_elsewhere(owner)
    monkeypatch.setattr(ses, "delete_identity", lambda d: (_ for _ in ()).throw(ses.MailOff("off")))
    assert _post(owner).status_code == 503
    assert MailDomain.objects.filter(pk=stale.pk).exists()


@pytest.mark.parametrize("status,age_days", [("pending", 6), ("verified", 30), ("suspended", 30)])
def test_a_fresh_pending_or_any_verified_holder_still_409s(owner, ws, monkeypatch, settings, status, age_days):
    settings.MAIL_PENDING_DOMAIN_TTL_DAYS = 7
    held = _held_elsewhere(owner, status=status, age_days=age_days)
    monkeypatch.setattr(ses, "delete_identity", lambda d: pytest.fail("deleted a live identity"))
    r = _post(owner)
    assert r.status_code == 409 and r.json()["detail"] == "that domain is already registered"
    assert MailDomain.objects.get(name="marshellis.com").pk == held.pk


def test_list_says_whether_the_caller_may_manage_even_with_no_domains(owner, ws):
    ed = User.objects.create_user("ed", "ed@x.com", "pw")
    WorkspaceMembership.objects.create(workspace=ws, user=ed, role="editor")
    assert _c(owner).get("/api/mail/domains/marshellis").json()["can_manage"] is True
    body = _c(ed).get("/api/mail/domains/marshellis").json()
    assert body["items"] == [] and body["can_manage"] is False


def test_re_adding_your_own_stale_claim_has_a_sensible_summary(owner, ws, monkeypatch, unverified):
    from apps.events.models import SettingChange
    stale = MailDomain.objects.create(workspace=ws, name="marshellis.com")
    MailDomain.objects.filter(pk=stale.pk).update(created_at=timezone.now() - dt.timedelta(days=8))
    assert _post(owner).status_code == 200
    row = SettingChange.objects.get()
    assert row.workspace == ws
    assert row.summary == "added marshellis.com; released a stale unverified claim on it"


def test_a_plain_add_keeps_the_default_audit_summary(owner, ws):
    from apps.events.models import SettingChange
    assert _post(owner).status_code == 200
    row = SettingChange.objects.get()
    assert row.changes == [{"scope": "mail.domain:marshellis.com", "before": None,
                            "after": {"name": "marshellis.com"}}]
    assert "released" not in row.summary
