"""Can a mail domain PROVE its senders? (canopy-web#1280)

An email is tied to a person only when it is DMARC-aligned or DKIM-signed by its
own From: domain (`Contact.EMAIL_ALIGNED`). A domain that publishes no DMARC
record and does not sign its own mail can never produce such a message, so an
interface rule that depends on one — `member@<domain>`, or anything
`@<domain>:verified` — admits nobody from it, and every member writing in from
it is silently treated as a contact.

This answers the question from two signals, either of which is enough:
  - `_dmarc.<domain>` publishes a `v=DMARC1` TXT record, or
  - aligned mail (`dmarc` / `dkim_aligned`) has actually been seen from that
    domain by a contact of this workspace.

Information only — it grades nothing and grants nothing. The DNS lookup is
time-boxed and cached, and every failure reads as UNKNOWN, never as "cannot
prove": a warning raised because a resolver hiccupped would be the kind of
noise that teaches people to ignore the real one.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from django.core.cache import cache

from .models import Contact

logger = logging.getLogger(__name__)

#: Seconds one DMARC lookup may take, all retries included. The interface save
#: has already happened by the time this runs; this bounds how long its response
#: (and a GET of the interface) can wait on DNS.
DNS_TIMEOUT = 1.0
#: How long an answer is reused. A record that exists rarely disappears; a
#: missing one is re-checked sooner so a fix shows up within the hour.
CACHE_FOUND = 6 * 3600
CACHE_MISSING = 3600
#: A lookup that could not be completed (timeout, SERVFAIL, no resolver).
CACHE_UNKNOWN = 300

_FOUND, _MISSING, _UNKNOWN = "found", "missing", "unknown"


@dataclass(frozen=True)
class DomainProof:
    domain: str
    #: True / False, or None when DNS could not be asked.
    dmarc: bool | None
    aligned_seen: bool

    @property
    def can_prove(self) -> bool | None:
        """True if either signal holds; None if DNS was unknown and no aligned
        mail has been seen (we cannot say); False only when both are known false."""
        if self.dmarc or self.aligned_seen:
            return True
        return None if self.dmarc is None else False


def _lookup_dmarc(domain: str) -> str:
    """One DNS query, no cache. `found` / `missing` / `unknown`."""
    try:
        import dns.exception
        import dns.resolver
    except ImportError:  # pragma: no cover — a direct dependency
        return _UNKNOWN
    try:
        answer = dns.resolver.resolve(f"_dmarc.{domain}", "TXT", lifetime=DNS_TIMEOUT)
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
        return _MISSING
    except (dns.exception.DNSException, OSError):
        return _UNKNOWN
    for rdata in answer:
        text = b"".join(getattr(rdata, "strings", ()) or ()).decode("ascii", "replace")
        if text.strip().lower().startswith("v=dmarc1"):
            return _FOUND
    return _MISSING


def dmarc_published(domain: str) -> bool | None:
    """Whether `_dmarc.<domain>` publishes a DMARC record. Cached; None = unknown."""
    domain = domain.strip().lower().rstrip(".")
    key = f"domain_proof:dmarc:{domain}"
    try:
        state = cache.get(key)
    except Exception:  # a cache outage must not turn into a failed request
        state = None
    if state is None:
        state = _lookup_dmarc(domain)
        ttl = {_FOUND: CACHE_FOUND, _MISSING: CACHE_MISSING}.get(state, CACHE_UNKNOWN)
        try:
            cache.set(key, state, ttl)
        except Exception:
            logger.warning("domain_proof: could not cache the DMARC answer for %s", domain)
    return {_FOUND: True, _MISSING: False}.get(state)


def aligned_mail_seen(domain: str, workspace_id) -> bool:
    """Whether a contact of this workspace at exactly `domain` has ever sent aligned
    mail. Per workspace, like contacts themselves: another tenant's mail is not
    this one's to read, even as a yes/no."""
    domain = domain.strip().lower()
    return Contact.objects.filter(
        workspace_id=workspace_id,
        email__iendswith=f"@{domain}",
        auth_result__in=list(Contact.EMAIL_ALIGNED),
    ).exists()


def proof(domain: str, workspace_id) -> DomainProof:
    seen = aligned_mail_seen(domain, workspace_id)
    # Seen aligned mail already answers it; do not spend a DNS query.
    return DomainProof(domain=domain, dmarc=None if seen else dmarc_published(domain),
                       aligned_seen=seen)
