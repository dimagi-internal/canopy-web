"""Member + invite management with owner RBAC.

Owners manage members and invites; editors/viewers cannot. Invites are accepted
by token, but only by the user whose email the invite names. The last owner
can't be removed.
"""
from __future__ import annotations

import datetime as dt
import json

import pytest
from django.contrib.auth import get_user_model
from django.core import mail
from django.test import Client, override_settings
from django.utils import timezone

from apps.workspaces.models import WorkspaceInvite, WorkspaceMembership

pytestmark = pytest.mark.django_db
User = get_user_model()


def _user(email):
    return User.objects.create(username=email, email=email)


def _client(u):
    c = Client()
    c.force_login(u)
    return c


def _post(c, url, data=None):
    return c.post(url, data=json.dumps(data or {}), content_type="application/json")


def _patch(c, url, data=None):
    return c.patch(url, data=json.dumps(data or {}), content_type="application/json")


def _ws(owner, slug="acme"):
    _post(_client(owner), "/api/workspaces/", {"slug": slug, "display_name": slug.title()})
    return slug


def _invite(owner, slug, email, role="editor"):
    return _post(_client(owner), f"/api/workspaces/{slug}/invites/", {"email": email, "role": role}).json()


def test_list_members_is_member_only():
    a = _user("a@dimagi.com")
    _ws(a)
    members = _client(a).get("/api/workspaces/acme/members/").json()
    assert len(members) == 1
    assert members[0]["email"] == "a@dimagi.com" and members[0]["role"] == "owner"
    b = _user("b@dimagi.com")
    assert _client(b).get("/api/workspaces/acme/members/").status_code == 404


def test_owner_invites_and_invitee_accepts():
    a = _user("a@dimagi.com")
    _ws(a)
    inv = _invite(a, "acme", "b@dimagi.com", "editor")
    assert inv["role"] == "editor" and inv["email"] == "b@dimagi.com" and inv["token"]
    b = _user("b@dimagi.com")
    r = _post(_client(b), f"/api/workspaces/invites/{inv['token']}/accept")
    assert r.status_code == 200, r.content
    assert r.json()["role"] == "editor"
    assert WorkspaceMembership.objects.get(workspace_id="acme", user=b).role == "editor"


def test_accept_requires_matching_email():
    a = _user("a@dimagi.com")
    _ws(a)
    inv = _invite(a, "acme", "b@dimagi.com")
    c = _user("c@dimagi.com")
    assert _post(_client(c), f"/api/workspaces/invites/{inv['token']}/accept").status_code == 403


def test_revoked_invite_cannot_be_accepted():
    a = _user("a@dimagi.com")
    _ws(a)
    inv = _invite(a, "acme", "b@dimagi.com")
    assert _post(_client(a), f"/api/workspaces/acme/invites/{inv['id']}/revoke").status_code == 204
    b = _user("b@dimagi.com")
    assert _post(_client(b), f"/api/workspaces/invites/{inv['token']}/accept").status_code == 410


def test_non_owner_cannot_invite():
    a = _user("a@dimagi.com")
    _ws(a)
    inv = _invite(a, "acme", "b@dimagi.com", "editor")
    b = _user("b@dimagi.com")
    _post(_client(b), f"/api/workspaces/invites/{inv['token']}/accept")  # b is now an editor
    assert _post(_client(b), "/api/workspaces/acme/invites/", {"email": "d@dimagi.com"}).status_code == 403


def test_remove_member_owner_only_and_protects_last_owner():
    a = _user("a@dimagi.com")
    _ws(a)
    inv = _invite(a, "acme", "b@dimagi.com", "editor")
    b = _user("b@dimagi.com")
    _post(_client(b), f"/api/workspaces/invites/{inv['token']}/accept")
    # an editor can't remove anyone
    assert _client(b).delete(f"/api/workspaces/acme/members/{a.id}/").status_code == 403
    # the owner can remove the editor
    assert _client(a).delete(f"/api/workspaces/acme/members/{b.id}/").status_code == 204
    # the last owner can't be removed — human-readable message, never the raw code
    r = _client(a).delete(f"/api/workspaces/acme/members/{a.id}/")
    assert r.status_code == 400
    assert r.json()["detail"] == "cannot remove the last owner"


def test_set_member_role_owner_only():
    a = _user("a@dimagi.com")
    _ws(a)
    inv = _invite(a, "acme", "b@dimagi.com", "editor")
    b = _user("b@dimagi.com")
    _post(_client(b), f"/api/workspaces/invites/{inv['token']}/accept")  # b is now an editor

    # a non-member is 404, never 403 (no existence leak)
    c = _user("c@dimagi.com")
    assert _patch(_client(c), f"/api/workspaces/acme/members/{b.id}/", {"role": "owner"}).status_code == 404

    # an editor (member, not owner) is 403
    assert _patch(_client(b), f"/api/workspaces/acme/members/{b.id}/", {"role": "owner"}).status_code == 403

    # the owner can promote the editor
    r = _patch(_client(a), f"/api/workspaces/acme/members/{b.id}/", {"role": "owner"})
    assert r.status_code == 200, r.content
    body = r.json()
    assert body["role"] == "owner" and body["user_id"] == b.id and body["email"] == "b@dimagi.com"
    assert WorkspaceMembership.objects.get(workspace_id="acme", user=b).role == "owner"


def test_set_member_role_target_not_a_member_is_404():
    a = _user("a@dimagi.com")
    _ws(a)
    ghost_id = 999999
    assert _patch(_client(a), f"/api/workspaces/acme/members/{ghost_id}/", {"role": "editor"}).status_code == 404


def test_set_member_role_cannot_demote_the_last_owner():
    a = _user("a@dimagi.com")
    _ws(a)
    r = _patch(_client(a), f"/api/workspaces/acme/members/{a.id}/", {"role": "editor"})
    assert r.status_code == 400
    # human-readable, and distinct from the sibling DELETE's message — never
    # the raw MemberError code ("last_owner") leaking into a user-facing banner
    assert r.json()["detail"] == "cannot demote the last owner"
    assert WorkspaceMembership.objects.get(workspace_id="acme", user=a).role == "owner"


def test_set_member_role_is_idempotent():
    a = _user("a@dimagi.com")
    _ws(a)
    r = _patch(_client(a), f"/api/workspaces/acme/members/{a.id}/", {"role": "owner"})
    assert r.status_code == 200, r.content
    assert r.json()["role"] == "owner"


def test_set_member_role_rejects_unknown_role():
    a = _user("a@dimagi.com")
    _ws(a)
    r = _patch(_client(a), f"/api/workspaces/acme/members/{a.id}/", {"role": "superuser"})
    assert r.status_code == 422


def _let_email_cool_down(invite_id):
    WorkspaceInvite.objects.filter(id=invite_id).update(
        last_emailed_at=timezone.now() - dt.timedelta(minutes=5)
    )


def test_owner_reissues_an_invite_and_only_the_new_link_works():
    a = _user("a@dimagi.com")
    _ws(a)
    inv = _invite(a, "acme", "b@dimagi.com")
    _let_email_cool_down(inv["id"])
    r = _post(_client(a), f"/api/workspaces/acme/invites/{inv['id']}/reissue")
    assert r.status_code == 200
    fresh = r.json()
    assert fresh["id"] == inv["id"] and fresh["token"] != inv["token"]
    assert fresh["invited_by_email"] == "a@dimagi.com"
    b = _user("b@dimagi.com")
    assert _post(_client(b), f"/api/workspaces/invites/{inv['token']}/accept").status_code == 404
    assert _post(_client(b), f"/api/workspaces/invites/{fresh['token']}/accept").status_code == 200


def test_reissue_is_owner_only_and_tenant_scoped():
    a = _user("a@dimagi.com")
    _ws(a)
    inv = _invite(a, "acme", "b@dimagi.com", "editor")
    b = _user("b@dimagi.com")
    _post(_client(b), f"/api/workspaces/invites/{inv['token']}/accept")  # b is now an editor
    pending = _invite(a, "acme", "c@dimagi.com")
    _let_email_cool_down(pending["id"])
    assert _post(_client(b), f"/api/workspaces/acme/invites/{pending['id']}/reissue").status_code == 403
    # another workspace's owner cannot reach acme's invite through their own slug
    d = _user("d@dimagi.com")
    _ws(d, "other")
    assert _post(_client(d), f"/api/workspaces/other/invites/{pending['id']}/reissue").status_code == 404


def test_reissue_of_a_revoked_invite_is_gone():
    a = _user("a@dimagi.com")
    _ws(a)
    inv = _invite(a, "acme", "b@dimagi.com")
    _post(_client(a), f"/api/workspaces/acme/invites/{inv['id']}/revoke")
    assert _post(_client(a), f"/api/workspaces/acme/invites/{inv['id']}/reissue").status_code == 410


@override_settings(CANOPY_PUBLIC_BASE_URL="https://labs.example.test/canopy")
def test_creating_an_invite_emails_the_link_to_the_invitee():
    a = _user("a@dimagi.com")
    _ws(a)
    mail.outbox.clear()
    inv = _invite(a, "acme", "b@partner.org", "viewer")

    assert inv["email_status"] == "sent"
    assert inv["last_emailed_at"]
    assert len(mail.outbox) == 1
    msg = mail.outbox[0]
    assert msg.to == ["b@partner.org"]
    assert msg.reply_to == ["a@dimagi.com"]
    assert "Acme" in msg.subject
    link = f"https://labs.example.test/canopy/invite/{inv['token']}"
    assert link in msg.body
    assert link in msg.alternatives[0][0]
    assert "b@partner.org" in msg.body  # tells them which Google account to use


def test_re_inviting_inside_a_minute_does_not_email_twice():
    a = _user("a@dimagi.com")
    _ws(a)
    mail.outbox.clear()
    first = _invite(a, "acme", "b@partner.org")
    again = _invite(a, "acme", "b@partner.org")
    assert again["id"] == first["id"]
    assert again["email_status"] == "throttled"
    assert len(mail.outbox) == 1


def test_reissue_emails_the_new_link_and_refuses_inside_a_minute():
    a = _user("a@dimagi.com")
    _ws(a)
    inv = _invite(a, "acme", "b@partner.org")
    # straight after the create email: refused BEFORE rotating, so the link
    # they were just sent keeps working
    assert _post(_client(a), f"/api/workspaces/acme/invites/{inv['id']}/reissue").status_code == 429
    assert WorkspaceInvite.objects.get(id=inv["id"]).token == inv["token"]

    _let_email_cool_down(inv["id"])
    mail.outbox.clear()
    fresh = _post(_client(a), f"/api/workspaces/acme/invites/{inv['id']}/reissue").json()
    assert fresh["email_status"] == "sent"
    assert len(mail.outbox) == 1 and fresh["token"] in mail.outbox[0].body


@override_settings(EMAIL_BACKEND="apps.common.email.NotConfiguredEmailBackend")
def test_invite_still_created_when_email_is_off():
    a = _user("a@dimagi.com")
    _ws(a)
    r = _post(_client(a), "/api/workspaces/acme/invites/", {"email": "b@partner.org"})
    assert r.status_code == 201
    assert r.json()["email_status"] == "not_configured"
    assert r.json()["last_emailed_at"] is None


def test_invite_still_created_when_the_send_fails(monkeypatch):
    from django.core.mail import EmailMultiAlternatives

    def boom(self, fail_silently=False):
        raise RuntimeError("SES said no")

    monkeypatch.setattr(EmailMultiAlternatives, "send", boom)
    a = _user("a@dimagi.com")
    _ws(a)
    r = _post(_client(a), "/api/workspaces/acme/invites/", {"email": "b@partner.org"})
    assert r.status_code == 201
    assert r.json()["email_status"] == "failed"
