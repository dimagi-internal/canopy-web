from __future__ import annotations

import pytest

from apps.mail import dns
from apps.mail.models import MailDomain


def _domain():
    return MailDomain(name="marshellis.com", dkim_tokens=["t1", "t2", "t3"])


def test_spf_is_merged_never_duplicated():
    assert dns.merged_spf("v=spf1 include:zohomail.com -all") == \
        "v=spf1 include:zohomail.com include:amazonses.com -all"
    assert dns.merged_spf("") == "v=spf1 include:amazonses.com -all"
    already = "v=spf1 include:amazonses.com ~all"
    assert dns.merged_spf(already) == already


def test_mx_provider_names_known_hosts():
    assert dns.mx_provider(["mx.zoho.com.", "mx2.zoho.com."]) == "Zoho"
    assert dns.mx_provider(["inbound-smtp.us-east-1.amazonaws.com."]) == "canopy (Amazon SES)"
    assert dns.mx_provider(["mx.example.net."]) == "mx.example.net"
    assert dns.mx_provider([]) == ""


def test_expected_records_cover_dkim_mailfrom_spf_dmarc_mx(monkeypatch):
    monkeypatch.setattr(dns, "_resolve", lambda name, rtype:
                        ["v=spf1 include:zohomail.com -all"] if (name, rtype) == ("marshellis.com", "TXT") else [])
    recs = {r.purpose: r for r in dns.expected_records(_domain())}
    assert recs["dkim-1"].name == "t1._domainkey.marshellis.com"
    assert recs["dkim-1"].value == "t1.dkim.amazonses.com"
    assert recs["mail-from-mx"].value == "feedback-smtp.us-east-1.amazonses.com"
    assert recs["spf"].value == "v=spf1 include:zohomail.com include:amazonses.com -all"
    assert recs["dmarc"].name == "_dmarc.marshellis.com"
    assert recs["mx"].value == "inbound-smtp.us-east-1.amazonaws.com" and recs["mx"].priority == 10


def test_check_marks_each_record(monkeypatch):
    answers = {("t1._domainkey.marshellis.com", "CNAME"): ["t1.dkim.amazonses.com."],
               ("marshellis.com", "MX"): ["10 mx.zoho.com."]}
    monkeypatch.setattr(dns, "_resolve", lambda n, t: answers.get((n, t), []))
    recs = {r.purpose: r for r in dns.check(_domain())}
    assert recs["dkim-1"].ok is True
    assert recs["dkim-2"].ok is False
    assert recs["mx"].ok is False and recs["mx"].found == ["10 mx.zoho.com."]
