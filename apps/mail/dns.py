"""The records a domain needs, and whether they are published. Read-only DNS:
canopy never writes anyone's zone."""
from __future__ import annotations

from dataclasses import dataclass, field

from django.conf import settings

_KNOWN_MX = {
    "zoho.com": "Zoho", "zoho.eu": "Zoho", "google.com": "Google", "googlemail.com": "Google",
    "outlook.com": "Microsoft 365", "amazonaws.com": "canopy (Amazon SES)",
    "mailgun.org": "Mailgun", "messagingengine.com": "Fastmail",
}


@dataclass
class DnsRecord:
    purpose: str
    type: str
    name: str
    value: str
    priority: int | None = None
    ok: bool | None = None
    found: list[str] = field(default_factory=list)


def _resolve(name: str, rtype: str) -> list[str]:
    import dns.exception
    import dns.resolver

    try:
        answer = dns.resolver.resolve(name, rtype, lifetime=4)
    except (dns.exception.DNSException, OSError):
        return []
    if rtype == "TXT":
        # A TXT rdata is a list of <=255-byte strings; the value is their concatenation.
        return [b"".join(r.strings).decode("utf-8", "replace") for r in answer]
    return [r.to_text() for r in answer]


_SPF_INCLUDE = "include:amazonses.com"
_SPF_ALL = {"all", "+all", "-all", "~all", "?all"}


def _is_spf(txt: str) -> bool:
    parts = txt.split()
    return bool(parts) and parts[0].lower() == "v=spf1"


def _spf_has_ses(txt: str) -> bool:
    return any(t.lower() == _SPF_INCLUDE for t in txt.split())


def merged_spf(existing: str) -> str:
    tokens = existing.split()
    if not tokens:
        return f"v=spf1 {_SPF_INCLUDE} -all"
    if _spf_has_ses(existing):
        return existing
    if tokens[-1].lower() in _SPF_ALL:
        return " ".join([*tokens[:-1], _SPF_INCLUDE, tokens[-1]])
    return f"{existing.strip()} {_SPF_INCLUDE}"


def mx_provider(hosts: list[str]) -> str:
    for h in hosts:
        host = h.split()[-1].rstrip(".").lower()
        for suffix, name in _KNOWN_MX.items():
            if host == suffix or host.endswith("." + suffix):
                return name
    return hosts[0].split()[-1].rstrip(".") if hosts else ""


def expected_records(domain) -> list[DnsRecord]:
    name = domain.name
    region = settings.MAIL_SES_REGION
    mail_from = f"{domain.mail_from_subdomain}.{name}"
    spf_all = [t for t in _resolve(name, "TXT") if _is_spf(t)]
    spf_now = spf_all[0] if spf_all else ""
    recs = [DnsRecord(f"dkim-{i}", "CNAME", f"{tok}._domainkey.{name}", f"{tok}.dkim.amazonses.com")
            for i, tok in enumerate(domain.dkim_tokens, start=1)]
    recs += [
        DnsRecord("mail-from-mx", "MX", mail_from, f"feedback-smtp.{region}.amazonses.com", priority=10),
        DnsRecord("mail-from-spf", "TXT", mail_from, "v=spf1 include:amazonses.com -all"),
        DnsRecord("spf", "TXT", name, merged_spf(spf_now), found=spf_all),
        DnsRecord("dmarc", "TXT", f"_dmarc.{name}", f"v=DMARC1; p=none; rua=mailto:dmarc@{name}"),
        DnsRecord("mx", "MX", name, settings.MAIL_INBOUND_MX, priority=10),
    ]
    return recs


def _matches(rec: DnsRecord, found: list[str]) -> bool:
    want = rec.value.rstrip(".").lower()
    if rec.type == "MX":
        return any(f.split()[-1].rstrip(".").lower() == want for f in found)
    if rec.purpose == "dmarc":
        return any(f.lower().startswith("v=dmarc1") for f in found)
    if rec.purpose in ("spf", "mail-from-spf"):
        return any(_is_spf(f) and _spf_has_ses(f) for f in found)
    return any(f.rstrip(".").lower() == want for f in found)


def check(domain) -> list[DnsRecord]:
    out = []
    for rec in expected_records(domain):
        rec.found = _resolve(rec.name, rec.type)
        rec.ok = _matches(rec, rec.found)
        out.append(rec)
    return out
