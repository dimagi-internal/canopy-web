import pytest
from django.contrib.auth.models import User
from django.test import Client, override_settings

from apps.tokens.models import AppCredential, DelegatedToken
from apps.workspaces import services as wsvc
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db

URL = "/api/auth/token-exchange"


@pytest.fixture()
def cred():
    admin = User.objects.create_user("admin", "admin@dimagi.com", "pw")
    raw, cred = AppCredential.create_credential(
        name="ace-web", domains=["dimagi.com"], created_by=admin)
    return raw, cred


def _post(raw_cred, email, **extra):
    return Client().post(
        URL, data={"acting_as_email": email, **extra},
        content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {raw_cred}")


def test_exchange_jit_provisions_and_mints(cred):
    raw, _ = cred
    resp = _post(raw, "newperson@dimagi.com")
    assert resp.status_code == 200, resp.content
    body = resp.json()
    tok = DelegatedToken.lookup(body["token"])
    assert tok is not None and tok.user.email == "newperson@dimagi.com"
    # the minted token authenticates a normal API call
    ok = Client().get("/api/me/", HTTP_AUTHORIZATION=f"Bearer {body['token']}")
    assert ok.status_code == 200


def test_exchange_rejects_foreign_domain(cred):
    raw, _ = cred
    assert _post(raw, "evil@attacker.org").status_code == 403


def test_exchange_rejects_bad_credential():
    assert _post("not-a-credential", "a@dimagi.com").status_code == 401


def test_exchange_rejects_personal_token(cred):
    from apps.tokens.models import PersonalToken
    user = User.objects.create_user("u2", "u2@dimagi.com", "pw")
    raw_pat, _ = PersonalToken.create_for_user(user=user, label="x")
    assert _post(raw_pat, "u2@dimagi.com").status_code == 401


def test_ttl_clamped(cred):
    raw, _ = cred
    resp = _post(raw, "p@dimagi.com", ttl_seconds=99999999)
    assert resp.status_code == 200
    tok = DelegatedToken.objects.latest("created_at")
    delta = (tok.expires_at - tok.created_at).total_seconds()
    assert delta <= 86400 + 5


# ──────────────────────────────────────────────────────────────────────
# F1: exchange must not resurrect a deactivated (offboarded) user
# ──────────────────────────────────────────────────────────────────────


def test_exchange_rejects_inactive_user(cred):
    raw, _ = cred
    user = User.objects.create_user("offboarded", "offboarded@dimagi.com", "pw")
    user.is_active = False
    user.save(update_fields=["is_active"])

    resp = _post(raw, "offboarded@dimagi.com")

    assert resp.status_code == 403
    assert resp.json()["detail"] == "delegation not allowed for this account"
    # no fresh delegated token was minted for the inactive user
    assert not DelegatedToken.objects.filter(user=user).exists()


def test_delegated_token_stops_authenticating_once_user_deactivated(cred):
    raw, _ = cred
    resp = _post(raw, "active@dimagi.com")
    assert resp.status_code == 200, resp.content
    dtoken = resp.json()["token"]

    # Works while active.
    ok = Client().get("/api/me/", HTTP_AUTHORIZATION=f"Bearer {dtoken}")
    assert ok.status_code == 200

    # Deactivate the user the token was minted for.
    user = User.objects.get(email="active@dimagi.com")
    user.is_active = False
    user.save(update_fields=["is_active"])

    # The previously-minted delegated token must stop authenticating REST.
    denied = Client().get("/api/me/", HTTP_AUTHORIZATION=f"Bearer {dtoken}")
    assert denied.status_code == 401


# ──────────────────────────────────────────────────────────────────────
# F4: the AppCredential allowlist and AUTH_ALLOWED_EMAIL_DOMAIN are two
# separate gates — a domain present in one but absent from the other must 403
# ──────────────────────────────────────────────────────────────────────


@override_settings(AUTH_ALLOWED_EMAIL_DOMAIN="dimagi.com")
def test_exchange_rejects_domain_absent_from_oauth_allowlist():
    admin = User.objects.create_user("admin2", "admin2@dimagi.com", "pw")
    # The AppCredential is scoped to "partner.org" — but AUTH_ALLOWED_EMAIL_DOMAIN
    # (the org-wide OAuth login policy) never includes it. Both gates must pass.
    raw, _ = AppCredential.create_credential(
        name="partner-app", domains=["partner.org"], created_by=admin)
    resp = _post(raw, "someone@partner.org")
    assert resp.status_code == 403


# ──────────────────────────────────────────────────────────────────────
# Tenant-scoped provisioning: AppCredential.provision_workspace /
# provision_role. The workspace is never client input — it comes only
# from the credential row (see docs/superpowers/plans/
# 2026-07-26-tenant-scoped-provisioning.md, Task 1).
# ──────────────────────────────────────────────────────────────────────


def _grant(cred, ws, role=WorkspaceMembership.EDITOR):
    cred.provision_workspace = ws
    cred.provision_role = role
    cred.save(update_fields=["provision_workspace", "provision_role"])
    return cred


def test_exchange_provisions_membership_in_granted_workspace(cred):
    raw, c = cred
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=c.created_by)
    _grant(c, ws, WorkspaceMembership.EDITOR)

    resp = _post(raw, "newperson@dimagi.com")
    assert resp.status_code == 200, resp.content
    assert resp.json()["workspace"] == "connect"

    user = User.objects.get(email="newperson@dimagi.com")
    memberships = WorkspaceMembership.objects.filter(user=user)
    assert memberships.count() == 1
    m = memberships.get()
    assert m.workspace_id == "connect"
    assert m.role == WorkspaceMembership.EDITOR


def test_exchange_without_provision_grants_no_membership(cred):
    raw, _ = cred
    resp = _post(raw, "noworkspace@dimagi.com")
    assert resp.status_code == 200, resp.content
    assert resp.json()["workspace"] is None

    user = User.objects.get(email="noworkspace@dimagi.com")
    assert WorkspaceMembership.objects.filter(user=user).count() == 0


def test_exchange_provisioning_never_raises_an_existing_members_role(cred):
    raw, c = cred
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=c.created_by)
    _grant(c, ws, WorkspaceMembership.EDITOR)

    user = User.objects.create_user("existing", "existing@dimagi.com", "pw")
    wsvc.ensure_member(ws, user, WorkspaceMembership.VIEWER)

    resp = _post(raw, "existing@dimagi.com")
    assert resp.status_code == 200, resp.content

    m = WorkspaceMembership.objects.get(workspace=ws, user=user)
    assert m.role == WorkspaceMembership.VIEWER  # not raised to editor


def test_exchange_provisioning_never_lowers_an_existing_members_role(cred):
    raw, c = cred
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=c.created_by)
    _grant(c, ws, WorkspaceMembership.VIEWER)

    user = User.objects.create_user("existing2", "existing2@dimagi.com", "pw")
    wsvc.ensure_member(ws, user, WorkspaceMembership.OWNER)

    resp = _post(raw, "existing2@dimagi.com")
    assert resp.status_code == 200, resp.content

    m = WorkspaceMembership.objects.get(workspace=ws, user=user)
    assert m.role == WorkspaceMembership.OWNER  # not lowered to viewer


def test_exchange_rejected_domain_provisions_nothing(cred):
    raw, c = cred
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=c.created_by)
    _grant(c, ws, WorkspaceMembership.EDITOR)

    resp = _post(raw, "evil@attacker.org")
    assert resp.status_code == 403
    assert not User.objects.filter(email="evil@attacker.org").exists()
    assert WorkspaceMembership.objects.filter(workspace=ws).count() == 0


def test_exchange_inactive_user_provisions_nothing(cred):
    raw, c = cred
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=c.created_by)
    _grant(c, ws, WorkspaceMembership.EDITOR)

    user = User.objects.create_user("offboarded2", "offboarded2@dimagi.com", "pw")
    user.is_active = False
    user.save(update_fields=["is_active"])

    resp = _post(raw, "offboarded2@dimagi.com")
    assert resp.status_code == 403
    assert WorkspaceMembership.objects.filter(workspace=ws, user=user).count() == 0
