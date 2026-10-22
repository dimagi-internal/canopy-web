"""`/api/me/alerts/` — list, act, undismiss, history.

A fake source (as in test_alerts_core) decides who is offered what, so these
pin the API's own rules: tenancy, the "no action, no row" visibility, ordering,
the shared dismiss, and 404-not-403 for what a caller cannot see.
"""
from __future__ import annotations

import datetime as dt
import sys
import types

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.utils import timezone

from apps.attention import alerts
from apps.attention.models import Alert, AlertEvent
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db

FAKE = "tests._fake_alert_source_api"
KIND = "fake_api_kind"
FIX = alerts.Action("fix", "Fix it", closes=True)
LINK = alerts.Action("open", "Open", closes=False, href="/somewhere")


@pytest.fixture()
def source(monkeypatch):
    mod = types.ModuleType(FAKE)
    mod.KIND = KIND
    mod.SEVERITY = Alert.ACTION
    mod.CONFIRM_WITHIN = None
    mod.CLOSING_ACTION_IDS = ("fix",)
    mod.offered = {}
    mod.performed = []
    mod.reconcile = lambda now: []
    mod.actions = lambda alert, user: list(mod.offered.get(user.pk, []))
    mod.perform = lambda alert, user, action_id: mod.performed.append((alert.pk, user.pk, action_id))
    monkeypatch.setitem(sys.modules, FAKE, mod)
    monkeypatch.setattr(alerts, "SOURCES", [FAKE])
    return mod


def _user(name):
    return User.objects.create_user(name, f"{name}@dimagi.com", "pw")


def _ws(slug, owner):
    ws = Workspace.objects.create(slug=slug, display_name=slug, created_by=owner)
    WorkspaceMembership.objects.create(user=owner, workspace=ws, role=WorkspaceMembership.OWNER)
    return ws


def _alert(ws, key="a", **kw):
    now = timezone.now()
    d = dict(kind=KIND, subject_key=key, workspace=ws, scopes=["workspace:" + ws.slug], severity=Alert.ACTION,
             title=f"Alert {key}", url="/x", first_seen_at=now, last_seen_at=now)
    d.update(kw)
    return Alert.objects.create(**d)


def _client(user):
    c = Client()
    c.force_login(user)
    return c


@pytest.fixture()
def world(source):
    ann, bob = _user("ann"), _user("bob")
    a = _ws("org-a", ann)
    WorkspaceMembership.objects.create(user=bob, workspace=a, role=WorkspaceMembership.OWNER)
    return types.SimpleNamespace(ann=ann, bob=bob, a=a, source=source, ca=_client(ann), cb=_client(bob))


def _ids(resp):
    assert resp.status_code == 200, resp.content
    return [r["id"] for r in resp.json()]


def test_list_shows_only_alerts_with_actions(world):
    shown, hidden = _alert(world.a, "shown"), _alert(world.a, "hidden")
    world.source.offered = {world.ann.pk: [FIX, LINK]}
    # a no-action viewer sees nothing; an offered one sees the row, with its actions
    assert _ids(world.cb.get("/api/me/alerts/")) == []
    rows = world.ca.get("/api/me/alerts/").json()
    assert {r["id"] for r in rows} == {shown.pk, hidden.pk}
    r = next(r for r in rows if r["id"] == shown.pk)
    assert r["actions"] == [{"id": "fix", "label": "Fix it", "closes": True, "href": ""},
                            {"id": "open", "label": "Open", "closes": False, "href": "/somewhere"}]
    assert r["organisation"] == "org-a" and r["workspace"] == "org-a" and r["last_notify"] == ""


def test_list_excludes_other_organisation(world, source):
    org_b = _ws("org-b", world.bob)
    world.source.offered = {world.bob.pk: [FIX]}
    a_alert = _alert(world.a, "in-a")  # bob IS in org-a here; remove him to test the boundary
    WorkspaceMembership.objects.filter(user=world.bob, workspace=world.a).delete()
    b_alert = _alert(org_b, "in-b")
    assert _ids(world.cb.get("/api/me/alerts/")) == [b_alert.pk]
    assert a_alert.pk not in _ids(world.cb.get("/api/me/alerts/"))


def test_scope_filter_and_ordering(world):
    old = timezone.now() - dt.timedelta(hours=1)
    setup = _alert(world.a, "s", severity=Alert.SETUP, scopes=["agent:eva"])
    blocking_old = _alert(world.a, "b1", severity=Alert.BLOCKING, last_seen_at=old, scopes=["agent:eva", "x"])
    blocking_new = _alert(world.a, "b2", severity=Alert.BLOCKING, scopes=["x"])
    act = _alert(world.a, "a", scopes=["agent:eva"])
    world.source.offered = {world.ann.pk: [FIX]}
    assert _ids(world.ca.get("/api/me/alerts/")) == [blocking_new.pk, blocking_old.pk, act.pk, setup.pk]
    assert _ids(world.ca.get("/api/me/alerts/?scope=agent:eva")) == [blocking_old.pk, act.pk, setup.pk]


def test_act_dismiss_clears_for_everyone(world):
    al = _alert(world.a)
    world.source.offered = {world.ann.pk: [FIX, alerts.DISMISS], world.bob.pk: [alerts.DISMISS]}
    resp = world.ca.post(f"/api/me/alerts/{al.pk}/act", {"action": "dismiss"}, content_type="application/json")
    assert resp.status_code == 200, resp.content
    assert resp.json()["status"] == "dismissed" and resp.json()["closed_by_email"] == "ann@dimagi.com"
    assert _ids(world.cb.get("/api/me/alerts/")) == [] and _ids(world.ca.get("/api/me/alerts/")) == []
    shown = world.cb.get("/api/me/alerts/?status=dismissed").json()
    assert [(r["id"], r["closed_by_email"]) for r in shown] == [(al.pk, "ann@dimagi.com")]
    # back it comes, for everyone
    resp = world.cb.post(f"/api/me/alerts/{al.pk}/undismiss")
    assert resp.status_code == 200 and resp.json()["status"] == "open"
    assert _ids(world.ca.get("/api/me/alerts/")) == [al.pk]


def test_dismissed_list_window_and_who(world):
    al = _alert(world.a)
    world.source.offered = {world.ann.pk: [alerts.DISMISS]}
    alerts.dismiss(al, world.ann)
    assert _ids(world.cb.get("/api/me/alerts/?status=dismissed")) == []  # bob is never offered dismiss
    assert _ids(world.ca.get("/api/me/alerts/?status=dismissed")) == [al.pk]
    Alert.objects.filter(pk=al.pk).update(closed_at=timezone.now() - dt.timedelta(days=8))
    assert _ids(world.ca.get("/api/me/alerts/?status=dismissed")) == []


def test_act_runs_handler_and_closes(world):
    al = _alert(world.a)
    world.source.offered = {world.ann.pk: [FIX]}
    resp = world.ca.post(f"/api/me/alerts/{al.pk}/act", {"action": "fix"}, content_type="application/json")
    assert resp.status_code == 200, resp.content
    assert resp.json()["status"] == "resolved" and resp.json()["actions"] == []
    assert world.source.performed == [(al.pk, world.ann.pk, "fix")]


def test_act_not_offered_409(world):
    al = _alert(world.a)
    world.source.offered = {world.ann.pk: [FIX, LINK]}
    for action in ("nope", "open"):  # unknown, and a link (followed in the browser, not run)
        resp = world.ca.post(f"/api/me/alerts/{al.pk}/act", {"action": action}, content_type="application/json")
        assert resp.status_code == 409, resp.content
        assert resp.json()["code"] == "action-not-offered"
    # a viewer offered nothing cannot even see it
    resp = world.cb.post(f"/api/me/alerts/{al.pk}/act", {"action": "fix"}, content_type="application/json")
    assert resp.status_code == 404


def test_undismiss_409_unless_offered_dismiss(world):
    al = _alert(world.a)
    world.source.offered = {world.ann.pk: [alerts.DISMISS], world.bob.pk: [FIX]}
    alerts.dismiss(al, world.ann)
    resp = world.cb.post(f"/api/me/alerts/{al.pk}/undismiss")  # bob cannot see a dismissed alert
    assert resp.status_code == 404
    open_one = _alert(world.a, "open-one")
    resp = world.ca.post(f"/api/me/alerts/{open_one.pk}/undismiss")  # not dismissed
    assert resp.status_code == 409 and resp.json()["code"] == "action-not-offered"


def test_history_visible_to_viewer_404_to_stranger(world):
    al = _alert(world.a)
    world.source.offered = {world.ann.pk: [FIX, alerts.DISMISS]}
    alerts.dismiss(al, world.ann)
    stranger = _client(_user("eve"))
    assert stranger.get(f"/api/me/alerts/{al.pk}/history").status_code == 404
    assert world.cb.get(f"/api/me/alerts/{al.pk}/history").status_code == 404  # in the org, but offered nothing
    resp = world.ca.get(f"/api/me/alerts/{al.pk}/history")
    assert resp.status_code == 200
    assert [(e["kind"], e["actor_email"]) for e in resp.json()] == [("dismissed", "ann@dimagi.com")]
    assert world.ca.get("/api/me/alerts/99999/history").status_code == 404


def test_last_notify_line(world):
    al = _alert(world.a)
    world.source.offered = {world.ann.pk: [FIX]}
    AlertEvent.objects.create(alert=al, at=timezone.now(), kind=AlertEvent.NOTIFIED, actor=world.ann,
                              payload={"to": [world.bob.pk], "by": world.ann.pk})
    line = world.ca.get("/api/me/alerts/").json()[0]["last_notify"]
    assert line.startswith("ann@dimagi.com asked bob@dimagi.com at ")
    AlertEvent.objects.create(alert=al, at=timezone.now() + dt.timedelta(seconds=1), kind=AlertEvent.NOTIFIED,
                              payload={"to": [world.bob.pk], "by": None})
    assert world.ca.get("/api/me/alerts/").json()[0]["last_notify"].startswith("Notified bob@dimagi.com at ")


def test_unknown_kind_row_is_skipped_not_500(world):
    _alert(world.a, "ghost", kind="no_such_kind")
    world.source.offered = {world.ann.pk: [FIX]}
    assert _ids(world.ca.get("/api/me/alerts/")) == []


def test_other_organisation_user_gets_404_on_every_by_id_route(world):
    """The tenancy check: offered everything by the source, but not in org-a."""
    al = _alert(world.a)
    carol = _user("carol")
    _ws("org-b", carol)
    world.source.offered = {carol.pk: [FIX, alerts.DISMISS]}
    cc = _client(carol)
    assert _ids(cc.get("/api/me/alerts/")) == []
    assert cc.post(f"/api/me/alerts/{al.pk}/act", {"action": "fix"}, content_type="application/json").status_code == 404
    assert cc.get(f"/api/me/alerts/{al.pk}/history").status_code == 404
    alerts.dismiss(al, world.ann)
    assert cc.post(f"/api/me/alerts/{al.pk}/undismiss").status_code == 404
    assert _ids(cc.get("/api/me/alerts/?status=dismissed")) == []
    Alert.objects.filter(pk=al.pk).update(status=Alert.RESOLVED)
    assert cc.get(f"/api/me/alerts/{al.pk}/history").status_code == 404


def test_history_of_a_resolved_alert(world):
    al = _alert(world.a)
    world.source.offered = {world.ann.pk: [FIX]}
    alerts.resolve(al, reason=Alert.REASON_SOURCE)
    assert _ids(world.ca.get("/api/me/alerts/")) == []
    resp = world.ca.get(f"/api/me/alerts/{al.pk}/history")
    assert resp.status_code == 200 and [e["kind"] for e in resp.json()] == ["resolved"]
    assert world.cb.get(f"/api/me/alerts/{al.pk}/history").status_code == 404  # offered nothing
    assert _client(_user("eve")).get(f"/api/me/alerts/{al.pk}/history").status_code == 404
