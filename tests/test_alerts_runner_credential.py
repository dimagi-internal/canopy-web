"""The `runner_credential` alert source: a signed-out runner credential, named by
the label a person gave it.

cloud-ec2-2, 2026-10-22: the token labelled "hal" (the box's `subscription-1`)
was signed out, and every surface called it "subscription-1" — a slot name its
owner never typed and could not map back to anything. The alert names it "hal".
"""
from __future__ import annotations

import datetime as dt

import pytest
from django.contrib.auth import get_user_model
from django.core import mail
from django.test import Client
from django.utils import timezone

from apps.agents.models import Agent
from apps.attention import alerts
from apps.attention.models import Alert
from apps.harness import alerts_runner_credential as src
from apps.harness import aws_iam_proof
from apps.harness.models import (
    Runner,
    RunnerAccessToken,
    RunnerAdmin,
    RunnerAssignment,
    RunnerDeviceKey,
    RunnerLogin,
)
from apps.agents.credentials import workspace_vaults
from apps.agents.models import Vault, VaultSecret
from apps.harness import runners as harness_runners
from apps.push import services as push_services
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db

SOURCE = "apps.harness.alerts_runner_credential"


def add_runner_credential(runner, *, kind, name=""):
    """A token or API key whose value the runner's vault holds — the only kind there is."""
    vault = harness_runners.open_runner_vault(runner)
    if vault is None:
        vault = workspace_vaults.create_for_runner(runner, by=runner.owner)
        Vault.objects.filter(pk=vault.pk).update(kv=1)
        vault.refresh_from_db()
    entry = harness_runners.add_runner_credential(runner, kind=kind, name=name)
    VaultSecret.objects.create(vault=vault, name=entry.vault_secret_name, value_zk="zk1:" + "A" * 60, kv=1)
    return entry
SIGNED_OUT = "not signed in — someone has to sign it in again · probed 2026-10-22T10:00:00Z"


def _user(name):
    return get_user_model().objects.create_user(username=name, email=f"{name}@dimagi.com",
                                                first_name=name.capitalize())


def _check(slot, status="fail", detail=SIGNED_OUT):
    return {"name": f"claude.probe.{slot}", "status": status, "detail": detail}


@pytest.fixture
def fleet(monkeypatch):
    jj, shayoni, ed, admin = _user("jj"), _user("shayoni"), _user("ed"), _user("ad")
    dimagi = Workspace.objects.create(slug="dimagi", display_name="Dimagi", created_by=jj)
    WorkspaceMembership.objects.create(workspace=dimagi, user=jj, role=WorkspaceMembership.OWNER)
    for u in (shayoni, ed, admin):
        WorkspaceMembership.objects.create(workspace=dimagi, user=u, role=WorkspaceMembership.EDITOR)
    runner = Runner.objects.create(name="cloud-ec2-2", kind=Runner.CLOUD, host="cloud-ec2-2", owner=jj,
                                   workspace=dimagi, status=Runner.ONLINE,
                                   last_heartbeat_at=timezone.now(), capabilities={},
                                   health={"checks": [_check("subscription-1")]})
    RunnerAdmin.objects.create(runner=runner, user=admin)
    # The first token on the list, named "hal" — what the status view reports
    # as `claude_token_label`.
    add_runner_credential(runner, kind=RunnerLogin.TOKEN, name="hal")
    eva = Agent.objects.create(slug="eva", name="Eva", workspace=dimagi, owner=shayoni)
    RunnerAssignment.objects.create(agent=eva, runner=runner, rank=0)
    monkeypatch.setattr(alerts, "SOURCES", [SOURCE])
    monkeypatch.setattr(alerts, "send_to_user", lambda user, title, body, url: False)
    return {"jj": jj, "shayoni": shayoni, "ed": ed, "admin": admin, "ws": dimagi,
            "runner": runner, "eva": eva}


def _set_checks(runner, *checks):
    runner.health = {"checks": list(checks)}
    runner.save(update_fields=["health"])


def _alert():
    return Alert.objects.get(kind=src.KIND)


# --- reconcile ----------------------------------------------------------------


def test_signed_out_probe_raises_with_label(fleet):
    runner = fleet["runner"]
    (r,) = src.reconcile(timezone.now())
    assert r.title == 'The "hal" token on cloud-ec2-2 no longer works'
    assert "subscription-1" not in r.title
    assert r.subject_key == f"runner:{runner.pk}:cred:subscription-1"
    assert r.workspace == "dimagi"
    assert r.url == f"/w/{runner.workspace_id}/runners/{runner.pk}"
    assert r.scopes == (f"runner:{runner.pk}", "workspace:dimagi", "agent:eva")
    # "hal" is cloud-ec2-2's only token: it can be replaced, not removed.
    assert r.body == "Claude rejects it. It is this runner's only token, so it can't be removed. Replace it with a new token."


def test_the_live_cli_wording_also_counts(fleet):
    """What the box's journal printed on cloud-ec2-2 — the CLI's own sentence."""
    _set_checks(fleet["runner"], _check("subscription-1", detail="Not logged in · Please run /login"))
    assert len(src.reconcile(timezone.now())) == 1


@pytest.mark.parametrize("check", [
    _check("subscription-1", status="ok", detail="answered a prompt · probed 2026-10-22T10:00:00Z"),
    _check("subscription-1", status="warn", detail="at its usage cap — resets 2026-10-23T00:00:00Z"),
    _check("subscription-1", status="fail", detail="exit 1: something else broke"),
    {"name": "claude.credentials", "status": "fail", "detail": "no Claude credential staged: not signed in"},
])
def test_only_a_signed_out_probe_raises(fleet, check):
    _set_checks(fleet["runner"], check)
    assert src.reconcile(timezone.now()) == []


def test_an_account_out_of_credit_raises_its_own_alert(fleet):
    """An API key with no credit never clears on its own either: the owner hears now."""
    _set_checks(fleet["runner"], _check(
        "subscription-1", detail="out of credit — someone has to add credit to its account"))
    (r,) = src.reconcile(timezone.now())
    assert r.title == 'The "hal" token on cloud-ec2-2 is out of credit'
    assert r.body.startswith("Its account has no credit left")
    assert "Add credit to its account" in r.body


def test_fingerprint_ignores_when_it_was_probed(fleet):
    (a,) = src.reconcile(timezone.now())
    _set_checks(fleet["runner"], _check("subscription-1", detail=SIGNED_OUT.replace("10:00", "10:10")))
    (b,) = src.reconcile(timezone.now())
    assert a.digest() == b.digest()
    _set_checks(fleet["runner"], _check("subscription-1", detail="Not logged in · Please run /login"))
    (c,) = src.reconcile(timezone.now())
    assert c.digest() != a.digest()


def test_retired_and_private_runners_are_skipped(fleet):
    runner = fleet["runner"]
    runner.status = Runner.RETIRED
    runner.save(update_fields=["status"])
    assert src.reconcile(timezone.now()) == []
    runner.status = Runner.ONLINE
    runner.workspace = None
    runner.save(update_fields=["status", "workspace"])
    assert src.reconcile(timezone.now()) == []


def test_unchanged_condition_writes_no_events_every_tick(fleet):
    t0 = timezone.now()
    alerts.tick(t0)
    _set_checks(fleet["runner"], _check("subscription-1", detail=SIGNED_OUT.replace("10:00", "10:10")))
    alerts.tick(t0 + dt.timedelta(minutes=1))
    assert [e.kind for e in _alert().events.order_by("pk")].count("changed") == 0


def test_probe_passing_resolves(fleet):
    t0 = timezone.now()
    alerts.tick(t0)
    assert _alert().status == Alert.OPEN
    _set_checks(fleet["runner"], _check("subscription-1", status="ok", detail="answered a prompt"))
    alerts.tick(t0 + dt.timedelta(minutes=1))
    a = _alert()
    assert a.status == Alert.RESOLVED and a.resolved_reason == Alert.REASON_SOURCE


# --- labels -------------------------------------------------------------------


def test_label_for_each_kind_of_slot(fleet):
    runner = fleet["runner"]
    assert src.label_for(runner, "subscription-1") == "hal"
    assert src.label_for(runner, "subscription-2") == "secondary token"
    tok = add_runner_credential(runner, kind=RunnerLogin.TOKEN, name="echo spare")
    assert src.label_for(runner, "subscription-2") == "echo spare"
    assert src.label_for(runner, f"token:{tok.key}") == "echo spare"
    RunnerLogin.objects.create(runner=runner, key="ab12cd34", kind=RunnerLogin.LOGIN,
                               account="hal@dimagi-ai.com", position=5)
    assert src.label_for(runner, "box-login:ab12cd34") == "hal@dimagi-ai.com"
    RunnerLogin.objects.create(runner=runner, key=RunnerLogin.DEFAULT, kind=RunnerLogin.LOGIN,
                               account="jj@dimagi.com", position=6)
    assert src.label_for(runner, "box-login") == "jj@dimagi.com"
    RunnerLogin.objects.create(runner=runner, key="ee55ff66", kind=RunnerLogin.LOGIN, position=7)
    assert src.label_for(runner, "box-login:ee55ff66") == "an unnamed login"
    assert src.label_for(runner, "api-key") == "API key"
    key = add_runner_credential(runner, kind=RunnerLogin.API_KEY, name="metered")
    assert src.label_for(runner, "api-key") == "metered"
    assert src.label_for(runner, f"api-key:{key.key}") == "metered"
    bare_tok = add_runner_credential(runner, kind=RunnerLogin.TOKEN)
    bare_key = add_runner_credential(runner, kind=RunnerLogin.API_KEY)
    assert src.label_for(runner, f"token:{bare_tok.key}") == "an unnamed token"
    assert src.label_for(runner, f"api-key:{bare_key.key}") == "an unnamed API key"
    assert src.label_for(runner, "mystery") == "mystery"


def test_a_box_login_is_titled_a_login(fleet):
    runner = fleet["runner"]
    RunnerLogin.objects.create(runner=runner, key="ab12cd34", kind=RunnerLogin.LOGIN,
                               account="ace@dimagi-ai.com", position=5)
    _set_checks(runner, _check("box-login:ab12cd34"))
    (r,) = src.reconcile(timezone.now())
    assert r.title == 'The "ace@dimagi-ai.com" login on cloud-ec2-2 no longer works'


def test_an_unnamed_first_token_is_the_primary_token(fleet):
    runner = fleet["runner"]
    RunnerLogin.objects.filter(runner=runner).update(name="")
    assert src.label_for(runner, "subscription-1") == "primary token"


# --- actions ------------------------------------------------------------------


def test_the_only_token_can_be_replaced_not_removed(fleet):
    """Removing a runner's last token is refused (LAST_FALLBACK_REFUSAL), so the
    alert never offers a button that would fail."""
    alerts.tick(timezone.now())
    a = _alert()
    for who in ("jj", "admin"):
        acts = src.actions(a, fleet[who])
        assert [(x.id, x.label) for x in acts] == [("replace", "Replace token")]
        assert acts[0].closes and acts[0].href == f"/w/{fleet['runner'].workspace_id}/runners/{fleet['runner'].pk}"


def test_a_token_with_a_backup_can_be_replaced_or_removed(fleet):
    runner = fleet["runner"]
    add_runner_credential(runner, kind=RunnerLogin.TOKEN, name="spare")
    (r,) = src.reconcile(timezone.now())
    assert r.body == "Claude rejects it. Replace it with a new token, or remove it."
    alerts.tick(timezone.now())
    acts = src.actions(_alert(), fleet["jj"])
    assert [(x.id, x.label, x.closes) for x in acts] == [
        ("replace", "Replace token", True), ("remove", "Remove token", True)]


def test_remove_drops_the_token_and_closes_the_alert(fleet):
    runner = fleet["runner"]
    add_runner_credential(runner, kind=RunnerLogin.TOKEN, name="spare")
    alerts.tick(timezone.now())
    alerts.act(_alert(), fleet["jj"], "remove")
    assert list(RunnerLogin.objects.filter(runner=runner, kind=RunnerLogin.TOKEN)
                .values_list("name", flat=True)) == ["spare"]
    a = _alert()
    assert a.status == Alert.RESOLVED and a.resolved_reason == Alert.REASON_ACTION
    # "spare" has moved up into subscription-1. The box's failing check for
    # that slot was about "hal": it is dropped, so it can't reopen the alert
    # against "spare" before the box probes again.
    runner.refresh_from_db()
    assert runner.health["checks"] == []
    alerts.tick(timezone.now() + dt.timedelta(minutes=1))
    assert _alert().status == Alert.RESOLVED


def test_remove_refused_when_it_became_the_last_token(fleet):
    runner = fleet["runner"]
    spare = add_runner_credential(runner, kind=RunnerLogin.TOKEN, name="spare")
    alerts.tick(timezone.now())
    a = _alert()
    RunnerLogin.objects.filter(pk=spare.pk).delete()
    with pytest.raises(alerts.ActionNotOffered):
        alerts.act(a, fleet["jj"], "remove")
    with pytest.raises(ValueError):
        src.perform(a, fleet["jj"], "remove")
    assert RunnerLogin.objects.filter(runner=runner, kind=RunnerLogin.TOKEN).count() == 1


def test_a_lapsed_login_offers_sign_in_again(fleet):
    runner = fleet["runner"]
    RunnerLogin.objects.create(runner=runner, key="ab12cd34", kind=RunnerLogin.LOGIN,
                               account="ace@dimagi-ai.com", position=5)
    _set_checks(runner, _check("box-login:ab12cd34"))
    (r,) = src.reconcile(timezone.now())
    assert r.body == "Its sign-in has lapsed. Sign it in again, or remove it."
    alerts.tick(timezone.now())
    acts = src.actions(_alert(), fleet["jj"])
    assert [(x.id, x.label) for x in acts] == [("replace", "Sign in again"), ("remove", "Remove login")]


def test_agent_owner_gets_notify_only(fleet):
    alerts.tick(timezone.now())
    a = _alert()
    (ask,) = src.actions(a, fleet["shayoni"])
    assert ask.id == "ask_owner" and not ask.closes
    assert ask.label == "Ask Jj to fix it"
    assert ask.notify == (fleet["jj"].pk,)
    assert src.actions(a, fleet["ed"]) == []  # a member with no stake sees nothing


def test_ask_owner_notifies_the_runner_owner(fleet):
    alerts.tick(timezone.now())
    mail.outbox.clear()
    alerts.act(_alert(), fleet["shayoni"], "ask_owner")
    assert [m.to for m in mail.outbox] == [["jj@dimagi.com"]]
    assert _alert().status == Alert.OPEN


def test_blocking_notice_goes_to_the_closers_only(fleet):
    alerts.tick(timezone.now())
    assert sorted(m.to[0] for m in mail.outbox) == ["ad@dimagi.com", "jj@dimagi.com"]
    assert 'The "hal" token on cloud-ec2-2 no longer works' in mail.outbox[0].subject


# --- the old mail -------------------------------------------------------------

ROLE = "arn:aws:iam::123456789012:role/canopy-cloud-runner-ec2-2-RunnerRole"


def test_auth_required_event_no_longer_mails(fleet, monkeypatch):
    """The runner reports a signed-out probe check, so the alert covers it: its
    own blocking notice replaces the once-a-day mail."""
    monkeypatch.setattr(push_services, "send_to_user", lambda *a, **k: 1)
    key = RunnerDeviceKey.objects.create(
        runner=fleet["runner"], kind=RunnerDeviceKey.AWS_IAM, role_arn=ROLE,
        jkt=aws_iam_proof.role_jkt(ROLE), status=RunnerDeviceKey.ACTIVE)
    raw, _ = RunnerAccessToken.mint(key)
    body = {"items": [{"source": "runner.credential", "kind": "claude_auth_required", "level": "error",
                       "key": "auth-required:cloud-ec2-2", "summary": "subscription-1 is not signed in"}]}
    res = Client().post("/api/events/", body, content_type="application/json",
                        HTTP_AUTHORIZATION=f"Bearer {raw}")
    assert res.status_code == 200, res.content
    assert mail.outbox == []
