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


@pytest.mark.parametrize("existing,expected", [
    ("v=spf1 include:a.com ?all", "v=spf1 include:a.com include:amazonses.com ?all"),
    ("v=spf1 include:a.com +all", "v=spf1 include:a.com include:amazonses.com +all"),
    ("v=spf1 include:a.com ~ALL", "v=spf1 include:a.com include:amazonses.com ~ALL"),
    ("v=spf1 redirect=_spf.example.com", "v=spf1 redirect=_spf.example.com include:amazonses.com"),
    ("v=spf1 include:a.com", "v=spf1 include:a.com include:amazonses.com"),
    ("v=spf1 include:spf.hall", "v=spf1 include:spf.hall include:amazonses.com"),
    ("V=SPF1 include:AmazonSES.com -all", "V=SPF1 include:AmazonSES.com -all"),
    ("v=spf1 include:notamazonses.com -all", "v=spf1 include:notamazonses.com include:amazonses.com -all"),
])
def test_merged_spf_edge_cases(existing, expected):
    assert dns.merged_spf(existing) == expected


class _Rdata:
    def __init__(self, strings):
        self.strings = strings


def test_resolve_joins_multi_string_txt(monkeypatch):
    import dns.resolver
    monkeypatch.undo()  # the autouse fixture stubbed _resolve; use the real one
    monkeypatch.setattr(dns.resolver, "resolve", lambda *a, **k: [_Rdata([b"v=spf1 include:a.com ", b"-all"])])
    from importlib import reload  # noqa: F401
    import apps.mail.dns as mod
    assert mod._resolve("x.com", "TXT") == ["v=spf1 include:a.com -all"]


def test_long_multipart_spf_is_merged_whole(monkeypatch):
    long_spf = "v=spf1 " + " ".join(f"include:s{i}.example.com" for i in range(20)) + " -all"
    monkeypatch.setattr(dns, "_resolve", lambda n, t: [long_spf] if (n, t) == ("marshellis.com", "TXT") else [])
    spf = {r.purpose: r for r in dns.expected_records(_domain())}["spf"]
    assert spf.value == long_spf.replace(" -all", " include:amazonses.com -all")


def test_uppercase_spf_is_recognised(monkeypatch):
    monkeypatch.setattr(dns, "_resolve", lambda n, t: ["V=SPF1 include:zohomail.com -all"] if t == "TXT" and n == "marshellis.com" else [])
    spf = {r.purpose: r for r in dns.expected_records(_domain())}["spf"]
    assert spf.value == "V=SPF1 include:zohomail.com include:amazonses.com -all"


def test_two_spf_records_both_surface_in_found(monkeypatch):
    first, second = "v=spf1 include:a.com -all", "v=spf1 include:b.com -all"
    monkeypatch.setattr(dns, "_resolve", lambda n, t: [first, second] if (n, t) == ("marshellis.com", "TXT") else [])
    recs = {r.purpose: r for r in dns.expected_records(_domain())}
    assert recs["spf"].found == [first, second]
    assert recs["spf"].value == "v=spf1 include:a.com include:amazonses.com -all"
    checked = {r.purpose: r for r in dns.check(_domain())}
    assert first in checked["spf"].found and second in checked["spf"].found


def test_check_spf_dmarc_and_mail_from_mx_rows(monkeypatch):
    answers = {
        ("marshellis.com", "TXT"): ["V=SPF1 include:AMAZONSES.com ~all"],
        ("_dmarc.marshellis.com", "TXT"): ["v=DMARC1; p=reject"],
        ("bounce.marshellis.com", "MX"): ["10 feedback-smtp.us-east-1.amazonses.com."],
    }
    monkeypatch.setattr(dns, "_resolve", lambda n, t: answers.get((n, t), []))
    recs = {r.purpose: r for r in dns.check(_domain())}
    assert recs["spf"].ok is True
    assert recs["dmarc"].ok is True
    assert recs["mail-from-mx"].ok is True
    assert recs["mail-from-spf"].ok is False
    assert recs["mx"].ok is False
