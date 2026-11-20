"""Disclosures to clients (HCP v2 7.4), use-until (7.6) and the disclosure summary (8.6).

What a context client receives is a *disclosure*, never the stored entry: content, the assertion,
only the categories its grant covers, a standing summary, a grant-scoped `ref` (never derivable
into the entry's id, different under every other grant), and `useUntil`: the time after which the
client may no longer use the copy without fetching it again. canopy cannot see what a client does
with a copy (3.11, invariant 14); use-until and revocation bound the harm and the audit log records
what was disclosed, which is what the summary is computed from.
"""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac

from django.conf import settings
from django.utils import timezone

from ..models import EntryRelationship, HcpRevocationDelivery, PersonAuditEvent, PersonFact, PersonGrant
from . import confirmations, rules
from .core import categories_of, entry_urn
from .entries import _iso
from .grants import categories_for
from .v2 import DECLARATION_V2, GENERAL_CONTEXT

# --- use-until (7.6) --------------------------------------------------------------------------
#: Starting values the spec says pilots will tune (7.6): the share of the entry's age a copy may be
#: used for, the floor and the ceilings, and the cap for a client with no way to be told of a revocation.
HEURISTIC_FRACTION = 0.10
FLOOR = dt.timedelta(hours=1)
CEILING = dt.timedelta(days=30)
CEILING_HIGH = dt.timedelta(hours=24)
NO_NOTIFICATION_CAP = dt.timedelta(hours=1)

#: 4.5: each registered category's default sensitivity. A custom category declares its own, and canopy
#: declares `medium` for every one it supports (never `low`, 4.5).
SENSITIVITY = {
    "general_preferences": "low", "goals_and_constraints": "low", "health_context": "high",
    "education_learner_profile": "medium", "work_context": "medium", "purchase_history_preferences": "low",
    "values_and_ethics": "medium", "location_context": "medium", "coordination_context": "medium",
}
_RANK = {"low": 0, "medium": 1, "high": 2}


def sensitivity_of(categories) -> str:
    """An entry's default sensitivity is the highest among its categories (4.5)."""
    return max((SENSITIVITY.get(c, "medium") for c in categories), key=_RANK.__getitem__, default="medium")


def has_notification_endpoint(grant: PersonGrant | None) -> bool:
    """6.2.3: a client that registered where to be told of a revocation. canopy's own agents have no
    such endpoint (a revoked grant fails their next read), so their copies are short."""
    client = grant.hcp_client if grant is not None and grant.hcp_client_id else None
    return bool(client is not None and client.webhook_url)


def use_until(fact: PersonFact, *, now: dt.datetime | None = None, notified: bool = True) -> dt.datetime:
    """When a copy disclosed now stops being usable (7.6).

    1. an explicit time (the entry's `validUntil`) applies first, even if earlier than the floor;
    2. else a heuristic: 10% of the time since the current version was recorded;
    3. a heuristic time is never less than 1 hour; no time is more than 30 days, or 24 hours when
       the entry's highest category sensitivity is High;
    4. a client with no notification endpoint gets at most 1 hour."""
    now = now or timezone.now()
    if fact.expires_at is not None:
        at = fact.expires_at
    else:
        age = max(now - fact.created_at, dt.timedelta(0))
        at = now + max(FLOOR, age * HEURISTIC_FRACTION)
    ceiling = CEILING_HIGH if sensitivity_of(categories_of(fact)) == "high" else CEILING
    at = min(at, now + ceiling)
    if not notified:
        at = min(at, now + NO_NOTIFICATION_CAP)
    return at


def longest_copy(categories, *, notified: bool) -> dt.timedelta:
    """The longest a copy may be used under a grant on these categories (told before approval, 6.1.6)."""
    cap = CEILING_HIGH if sensitivity_of(categories) == "high" else CEILING
    return min(cap, NO_NOTIFICATION_CAP) if not notified else cap


# --- the disclosure (7.4) -----------------------------------------------------------------------


def ref_for(grant: PersonGrant, entry_id) -> str:
    """Opaque, stable for one grant and one entry, different under every other grant, and not
    derivable into the entry's id (a keyed hash)."""
    key = hmac.new(settings.SECRET_KEY.encode(), b"hcp-v2-entry-ref", hashlib.sha256).digest()
    mac = hmac.new(key, f"{grant.grant_id}:{entry_id}".encode(), hashlib.sha256).digest()[:18]
    return base64.urlsafe_b64encode(mac).decode().rstrip("=")


def to_disclosure(fact: PersonFact, grant: PersonGrant, *, now: dt.datetime | None = None,
                  refs: dict | None = None) -> dict:
    """The entry as a context client receives it. `refs` maps entry id → ref for the entries of the
    same response: a relationship is shown only where its target is among them."""
    covered = set(categories_for(grant, "read"))
    assertion = {"declarationType": DECLARATION_V2[fact.basis], "confidence": fact.confidence or None,
                 "validUntil": _iso(fact.expires_at)}
    if fact.basis_detail:
        assertion["basis"] = dict(fact.basis_detail)
    if fact.permitted_purposes is not None:
        assertion["permittedPurposes"] = list(fact.permitted_purposes)
    classification = {"categories": [c for c in categories_of(fact) if c in covered],
                      "dimension": fact.dimension or None}
    if fact.restricted_purposes is not None:
        classification["permittedPurposes"] = list(fact.restricted_purposes)
    out = {
        "ref": ref_for(grant, fact.entry_id), "version": fact.version,
        "content": {"profile": GENERAL_CONTEXT, "value": fact.statement, "language": None},
        "evidence": [], "assertion": assertion, "classification": classification,
        "standing": {"assurance": fact.assurance, "confirmed": confirmations.is_confirmed(fact)},
        "useUntil": _iso(use_until(fact, now=now, notified=has_notification_endpoint(grant))),
    }
    if refs:
        rels = [{"type": r.type, "target": refs[r.target_entry_id]}
                for r in EntryRelationship.objects.filter(person_id=fact.person_id, entry_id=fact.entry_id,
                                                          target_entry_id__in=list(refs))]
        if rels:
            out["relationships"] = rels
    return out


def resolve_ref(grant: PersonGrant, ref: str, candidates) -> PersonFact | None:
    """The entry a `ref` names under this grant, among `candidates`; another grant's ref names nothing."""
    for f in candidates:
        if hmac.compare_digest(ref_for(grant, f.entry_id), ref or ""):
            return f
    return None


# --- the disclosure summary (8.6) ----------------------------------------------------------------


def _client_of(grant: PersonGrant) -> dict:
    client = grant.hcp_client if grant.hcp_client_id else None
    if client is not None:
        return {"id": client.client_id, "name": client.name,
                "operator": {"id": rules.operator_of(grant)[0], "name": client.operator}}
    slug = grant.agent.slug if grant.agent_id else grant.client_key
    return {"id": f"agent:{slug}", "name": (grant.agent.name or slug) if grant.agent_id else grant.client_name,
            "operator": {"id": rules.PROVIDER_OPERATOR, "name": "canopy"}}


def summary(person, *, client: str | None = None, category: str | None = None,
            since: dt.datetime | None = None, until: dt.datetime | None = None) -> list[dict]:
    """What each client has received, computed from the audit log and nothing else (8.6): its grants,
    the categories and purposes, how many distinct entries and retrievals, the period, the latest
    use-until among the copies (or, where a revocation notice was delivered, when), the notification
    outcome and a declared legal retention. It does not show what a client did with a copy."""
    events = (PersonAuditEvent.objects.filter(person=person, event_type="preference.read",
                                              grant_id__isnull=False).order_by("pk"))
    if category:
        events = events.filter(category=category)
    if since:
        events = events.filter(timestamp__gte=since)
    if until:
        events = events.filter(timestamp__lte=until)
    grants = {g.grant_id: g for g in PersonGrant.objects.filter(person=person)
              .select_related("agent", "hcp_client")}
    by_client: dict[str, dict] = {}
    for e in events:
        grant = grants.get(e.grant_id)
        if grant is None:
            continue
        who = _client_of(grant)
        if client and who["id"] != client:
            continue
        row = by_client.setdefault(who["id"], {
            "client": who, "grants": {}, "categories": set(), "purposes": set(), "entries": set(),
            "retrievals": set(), "firstAt": e.timestamp, "lastAt": e.timestamp, "useUntil": [], "_grants": []})
        row["grants"][grant.grant_id] = grant
        if e.category:
            row["categories"].add(e.category)
        if e.purpose_code:
            row["purposes"].add(e.purpose_code)
        if e.entry_id:
            row["entries"].add(e.entry_id)
        row["retrievals"].add((e.data or {}).get("retrievalId") or e.pk)
        row["firstAt"], row["lastAt"] = min(row["firstAt"], e.timestamp), max(row["lastAt"], e.timestamp)
        if (e.data or {}).get("useUntil"):
            row["useUntil"].append(dt.datetime.fromisoformat(e.data["useUntil"]))
    out = []
    for row in by_client.values():
        grant_list = list(row["grants"].values())
        deliveries = list(HcpRevocationDelivery.objects.filter(grant__in=grant_list).order_by("-pk"))
        notification, usable = "none", max(row["useUntil"], default=None)
        if deliveries:
            last = deliveries[0]
            notification = ("delivered" if last.delivered_at else "abandoned" if last.abandoned_at else "none")
            if last.delivered_at:
                usable = last.delivered_at                      # a delivered notice ends every copy (6.2.2)
        item = {
            "client": row["client"],
            "grants": [{"grantId": entry_urn(g.grant_id), "status": g.status} for g in grant_list],
            "categories": sorted(row["categories"]), "purposes": sorted(row["purposes"]),
            "entries": len(row["entries"]), "retrievals": len(row["retrievals"]),
            "firstAt": _iso(row["firstAt"]), "lastAt": _iso(row["lastAt"]),
            "usableUntil": _iso(usable), "notification": notification,
        }
        retention = next((g.hcp_client.legal_retention for g in grant_list
                          if g.hcp_client_id and g.hcp_client.legal_retention), None)
        if retention:
            item["legalRetention"] = dict(retention)
        out.append(item)
    return out
