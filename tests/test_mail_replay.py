from __future__ import annotations

from io import StringIO
from pathlib import Path

import pytest
from botocore.exceptions import ClientError
from django.contrib.auth.models import User
from django.core.management import CommandError, call_command
from django.db import OperationalError

from apps.agents.models import Agent
from apps.mail import inbound, ses
from apps.mail.models import MailDomain, MailRoute
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db
FIX = Path(__file__).parent / "fixtures" / "mail"

RAW = (b"From: Some Person <someone@example.org>\r\n"
       b"To: ada@marshellis.com, someone-else@example.org\r\n"
       b"Cc: apple@marshellis.com\r\n"
       b"Delivered-To: bob@marshellis.com\r\n"
       b"X-SES-Spam-Verdict: PASS\r\nX-SES-Virus-Verdict: PASS\r\n"
       b"Subject: Hello\r\nMessage-ID: <replay-1@example.org>\r\n\r\nHi\r\n")


@pytest.fixture()
def world(settings, monkeypatch):
    settings.MAIL_ENABLED = True
    u = User.objects.create_user("jj", "jjackson@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="marshellis", display_name="M", created_by=u)
    WorkspaceMembership.objects.create(workspace=ws, user=u, role="owner")
    d = MailDomain.objects.create(workspace=ws, name="marshellis.com", status="verified")
    a = Agent.objects.create(slug="mada", name="Ada", workspace=ws)
    MailRoute.objects.create(domain=d, local_part="ada", kind="agent", agent=a)
    MailRoute.objects.create(domain=d, local_part="*", kind="forward", forward_to=u)
    calls, reads = [], []
    monkeypatch.setattr(inbound, "deliver_agent",
                        lambda r, p, k, **kw: calls.append(("agent", r.address, k, kw["verdicts"])) or "agent")
    monkeypatch.setattr(inbound, "deliver_forward",
                        lambda r, p, raw, rcpt: calls.append(("fwd", rcpt)) or "forwarded")
    world = type("W", (), {"calls": calls, "reads": reads, "raw": RAW})
    monkeypatch.setattr(ses, "get_object", lambda key: reads.append(key) or world.raw)
    return world


def run(*args) -> str:
    out = StringIO()
    call_command("mail_replay", *args, stdout=out)
    return out.getvalue()


def test_replay_defaults_to_our_to_cc_and_delivered_to_recipients(world):
    out = run("incoming/abc")
    assert out.splitlines() == ["ada@marshellis.com: agent", "apple@marshellis.com: forwarded",
                                "bob@marshellis.com: forwarded"]
    assert world.reads == ["incoming/abc"]
    assert world.calls[0] == ("agent", "ada@marshellis.com", "incoming/abc", {"spam": "PASS", "virus": "PASS"})


def test_recipient_overrides_the_defaults(world):
    assert run("incoming/abc", "--recipient", "Ada@marshellis.com").splitlines() == ["ada@marshellis.com: agent"]
    assert [c[0] for c in world.calls] == ["agent"]


def test_dry_run_delivers_nothing(world):
    out = run("incoming/abc", "--dry-run", "--recipient", "ada@marshellis.com",
              "--recipient", "x@marshellis.com", "--recipient", "y@other.org")
    assert out.splitlines() == ["ada@marshellis.com: would:agent mada",
                                "x@marshellis.com: would:forward to jjackson@dimagi.com",
                                "y@other.org: would:dropped:unknown-domain"]
    assert world.calls == []


@pytest.mark.parametrize("key", ["kept/abc", "other/abc", "incoming/../kept/abc"])
def test_keys_outside_incoming_are_refused_before_s3(world, key):
    with pytest.raises(CommandError, match="refused key"):
        run(key)
    assert world.reads == []


def test_spam_stays_dropped_on_replay(world):
    world.raw = RAW.replace(b"X-SES-Spam-Verdict: PASS", b"X-SES-Spam-Verdict: FAIL")
    assert run("incoming/abc", "--recipient", "ada@marshellis.com").strip() == "ada@marshellis.com: dropped:spam"
    assert world.calls == []


def test_no_recipient_on_our_domains_is_an_error(world):
    world.raw = (FIX / "plain.eml").read_bytes().replace(b"ada@marshellis.com", b"x@else.org")
    with pytest.raises(CommandError, match="--recipient"):
        run("incoming/abc")


def test_mail_off_and_expired_object_are_errors(world, settings, monkeypatch):
    def gone(key):
        raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
    monkeypatch.setattr(ses, "get_object", gone)
    with pytest.raises(CommandError, match="expire after 7 days"):
        run("incoming/abc")
    monkeypatch.setattr(ses, "get_object", lambda key: (_ for _ in ()).throw(ses.MailOff("off")))
    with pytest.raises(CommandError, match="off"):
        run("incoming/abc")


def test_transient_failure_prints_outcomes_then_errors(world, monkeypatch):
    def flaky(r, p, k, **kw):
        raise OperationalError("db gone")
    monkeypatch.setattr(inbound, "deliver_agent", flaky)
    out = StringIO()
    with pytest.raises(CommandError, match="run it again"):
        call_command("mail_replay", "incoming/abc", stdout=out)
    assert "ada@marshellis.com: error:OperationalError" in out.getvalue()
    assert "apple@marshellis.com: forwarded" in out.getvalue()


def test_a_real_replay_of_delivered_agent_mail_is_a_duplicate(world, monkeypatch):
    # Not stubbed: the real deliver path dedupes, which is what makes replay safe.
    from apps.mail import agent_mail
    monkeypatch.setattr(inbound, "deliver_agent",
                        lambda r, p, k, **kw: agent_mail.deliver(r, p, k, **kw))
    monkeypatch.setattr(ses, "keep", lambda k: "kept/" + k.split("/", 1)[-1])
    lines = run("incoming/abc", "--recipient", "ada@marshellis.com").splitlines()
    assert lines == ["ada@marshellis.com: agent"]
    assert run("incoming/abc", "--recipient", "ada@marshellis.com").splitlines() == [
        "ada@marshellis.com: duplicate"]
