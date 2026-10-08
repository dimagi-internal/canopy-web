"""An interface rule naming a mail domain that can never prove its senders is
WARNED about, never refused (canopy-web#1280).

`member@<domain>` and `…@<domain>:verified` are satisfied by email only when the
message is DMARC-aligned or DKIM-signed by its own From: domain. A domain with no
DMARC record and no aligned DKIM can never produce that, so the rule silently
admits nobody and every member writing in from it is treated as a contact.
"""
from __future__ import annotations

import time

import pytest
from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import Client

from apps.agents.interface import _rules_needing_proof, domain_warnings, parse
from apps.agents.models import Agent
from apps.contacts import domain_proof
from apps.contacts.models import Contact
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db
M = WorkspaceMembership

SOURCE = """\
full: [member@nodmarc.org, contact@dimagi.com:verified]
capabilities:
  ask:
    callers: [contact, contact@nodmarc.org, member@signs.org:verified]
"""


@pytest.fixture(autouse=True)
def _clean_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture()
def dns(monkeypatch):
    """A fake resolver: {domain: 'found'|'missing'|'unknown'}, recording queries."""
    answers: dict[str, str] = {}
    asked: list[str] = []

    def lookup(domain):
        asked.append(domain)
        return answers.get(domain, "missing")

    monkeypatch.setattr(domain_proof, "_lookup_dmarc", lookup)
    return answers, asked


@pytest.fixture()
def w():
    op = User.objects.create_user("op", "op@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=op)
    M.objects.create(user=op, workspace=ws, role=M.OWNER)
    agent = Agent.objects.create(slug="ace", name="Ace", workspace=ws, owner=op)
    c = Client()
    c.force_login(op)
    return {"op": op, "ws": ws, "agent": agent, "client": c}


def test_only_rules_that_need_mail_proof_are_probed():
    iface = parse({"full": ["member@a.org", "contact@b.org", "contact@c.org:verified"],
                   "capabilities": {"ask": {"callers": ["member", "contact:verified",
                                                        "member@a.org", "member@d.org:verified"]}}})
    assert _rules_needing_proof(iface) == {
        "a.org": ["member@a.org"],
        "c.org": ["contact@c.org:verified"],
        "d.org": ["member@d.org:verified"],
    }


def test_publishing_warns_about_a_domain_that_cannot_prove_itself(w, dns):
    answers, _ = dns
    answers.update({"dimagi.com": "found", "signs.org": "missing"})
    # signs.org has no DMARC but its mail IS DKIM-signed by itself: seen aligned.
    Contact.objects.create(workspace=w["ws"], email="x@signs.org",
                           auth_result=Contact.AUTH_DKIM_ALIGNED)
    r = w["client"].put("/api/agents/ace/interface", {"source": SOURCE},
                        content_type="application/json")
    assert r.status_code == 200, r.content
    warnings = r.json()["warnings"]
    assert len(warnings) == 1
    msg = warnings[0]
    assert msg.startswith("member@nodmarc.org:")
    # A plain `contact@` rule needs no proof, so it is not named.
    assert "contact@nodmarc.org" not in msg
    assert "_dmarc.nodmarc.org" in msg and "DKIM" in msg
    # Saved regardless — a warning, not a refusal.
    w["agent"].refresh_from_db()
    assert w["agent"].interface["full"] == ["member@nodmarc.org", "contact@dimagi.com:verified"]
    # And it is still there when the interface is read back.
    assert w["client"].get("/api/agents/ace/interface").json()["warnings"] == warnings


def test_aligned_mail_seen_answers_without_dns(w, dns):
    _, asked = dns
    Contact.objects.create(workspace=w["ws"], email="a@nodmarc.org", auth_result=Contact.AUTH_DMARC)
    assert domain_warnings(parse({"full": ["member@nodmarc.org"]}), w["ws"].pk) == []
    assert asked == []


def test_another_workspaces_mail_is_not_evidence(w, dns):
    other = Workspace.objects.create(slug="other", display_name="Other", created_by=w["op"])
    Contact.objects.create(workspace=other, email="a@nodmarc.org", auth_result=Contact.AUTH_DMARC)
    assert len(domain_warnings(parse({"full": ["member@nodmarc.org"]}), w["ws"].pk)) == 1


def test_unaligned_mail_seen_is_not_evidence(w, dns):
    Contact.objects.create(workspace=w["ws"], email="a@nodmarc.org", auth_result=Contact.AUTH_DKIM)
    assert len(domain_warnings(parse({"full": ["member@nodmarc.org"]}), w["ws"].pk)) == 1


def test_a_dns_failure_is_unknown_and_raises_no_warning(w, dns):
    answers, _ = dns
    answers["nodmarc.org"] = "unknown"
    assert domain_warnings(parse({"full": ["member@nodmarc.org"]}), w["ws"].pk) == []


def test_answers_are_cached(w, dns):
    _, asked = dns
    iface = parse({"full": ["member@nodmarc.org"]})
    domain_warnings(iface, w["ws"].pk)
    domain_warnings(iface, w["ws"].pk)
    assert asked == ["nodmarc.org"]


def test_a_broken_probe_never_fails_a_save(w, monkeypatch):
    def boom(domain):
        raise RuntimeError("resolver exploded")

    monkeypatch.setattr(domain_proof, "_lookup_dmarc", boom)
    r = w["client"].put("/api/agents/ace/interface", {"source": SOURCE},
                        content_type="application/json")
    assert r.status_code == 200 and r.json()["warnings"] == []


def test_the_dns_budget_bounds_a_slow_resolver(w, monkeypatch):
    from apps.agents import interface

    monkeypatch.setattr(interface, "_PROOF_BUDGET_SECONDS", 0.05)
    asked = []

    def slow(domain):
        asked.append(domain)
        time.sleep(0.1)
        return "missing"

    monkeypatch.setattr(domain_proof, "_lookup_dmarc", slow)
    iface = parse({"full": ["member@a.org", "member@b.org", "member@c.org"]})
    domain_warnings(iface, w["ws"].pk)
    assert len(asked) == 1


def test_no_interface_means_no_warnings_and_no_probe(w, dns):
    _, asked = dns
    assert w["client"].get("/api/agents/ace/interface").json()["warnings"] == []
    assert asked == []


def test_the_real_lookup_reads_the_dmarc_txt(monkeypatch):
    """`_lookup_dmarc` against a stubbed dnspython: v=DMARC1 → found; other TXT →
    missing; NXDOMAIN → missing; timeout → unknown."""
    import dns.exception
    import dns.resolver

    monkeypatch.undo()   # the autouse fixture stubbed _lookup_dmarc itself

    class R:
        def __init__(self, s):
            self.strings = [s]

    def fake(answer):
        def resolve(name, rtype, lifetime):
            assert name.startswith("_dmarc.") and rtype == "TXT" and lifetime <= 1.0
            if isinstance(answer, Exception):
                raise answer
            return [R(answer)]
        return resolve

    cases = [(b"v=DMARC1; p=none", "found"), (b"google-site-verification=x", "missing"),
             (dns.resolver.NXDOMAIN(), "missing"), (dns.exception.Timeout(), "unknown")]
    for answer, want in cases:
        monkeypatch.setattr(dns.resolver, "resolve", fake(answer))
        assert domain_proof._lookup_dmarc("x.org") == want
