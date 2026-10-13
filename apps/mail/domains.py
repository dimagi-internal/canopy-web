# apps/mail/domains.py
"""Domains and routes: what an admin configures, and the address → route answer."""
from __future__ import annotations

import datetime as dt
import logging
import re

from botocore.exceptions import BotoCoreError, ClientError
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.agents.models import Agent
from apps.workspaces import services as wsvc

from . import dns, ses
from .models import MailDomain, MailRoute

_DOMAIN_RE = re.compile(
    r"^(?=.{1,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+([a-z]{2,63}|xn--[a-z0-9-]{1,59})$")
_LOCAL_RE = re.compile(r"^(\*|[a-z0-9!#$%&'+/=?^_`{|}~-]+(\.[a-z0-9!#$%&'+/=?^_`{|}~-]+)*)$")
_LOCAL_MAX = 64


log = logging.getLogger(__name__)

_CHECK_TTL = 60  # seconds a DNS check is reused
_FORCE_COOLDOWN = 10  # seconds between forced refreshes of one domain


class DomainError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def add_domain(workspace, name: str, *, by) -> MailDomain:
    name = (name or "").strip().lower().rstrip(".")
    if not _DOMAIN_RE.match(name):
        raise DomainError(422, "that is not a domain name")
    if any(name == r or name.endswith("." + r)
           for r in (x.strip().lower().rstrip(".") for x in settings.MAIL_RESERVED_DOMAINS) if r):
        raise DomainError(422, "that domain is reserved")
    holder = MailDomain.objects.filter(name=name).first()
    if holder is not None:
        if not _stale_pending(holder):
            raise DomainError(409, "that domain is already registered")
        _release(holder)
    try:
        with transaction.atomic():
            d = MailDomain.objects.create(workspace=workspace, name=name, created_by=by)
    except IntegrityError:
        raise DomainError(409, "that domain is already registered") from None
    created = False
    try:
        d.dkim_tokens = ses.create_identity(name, mail_from=f"{d.mail_from_subdomain}.{name}")
        created = True
        d.save(update_fields=["dkim_tokens"])
        return refresh(d, force=True)
    except Exception as exc:
        _undo_add(d, created=created)
        if isinstance(exc, ses.MailOff):
            raise DomainError(503, str(exc)) from None
        if isinstance(exc, ses.IdentityExists):
            raise DomainError(409, "that domain is already registered with the mail provider") from None
        if isinstance(exc, (ClientError, BotoCoreError)):
            raise DomainError(502, f"SES refused: {_reason(exc)}") from None
        raise


def _stale_pending(d: MailDomain) -> bool:
    """A domain still pending DNS after the TTL is abandoned: it never proved control, so
    it may not hold the name against anyone (in any workspace) any longer."""
    ttl = dt.timedelta(days=settings.MAIL_PENDING_DOMAIN_TTL_DAYS)
    return d.status == MailDomain.PENDING and d.created_at < timezone.now() - ttl


def _release(d: MailDomain) -> None:
    """Remove an abandoned pending domain: its SES identity (canopy created it for this row)
    best effort, then the row and its routes. Threads stay (route → NULL)."""
    try:
        ses.delete_identity(d.name)
    except ses.MailOff as exc:
        raise DomainError(503, str(exc)) from None
    except Exception:  # noqa: BLE001 - best effort: the stale row must not hold the name
        log.warning("could not delete SES identity %s while releasing it", d.name, exc_info=True)
    cache.delete(_cache_key(d))
    log.info("released stale pending domain %s (workspace %s, added %s)",
             d.name, d.workspace_id, d.created_at.isoformat())
    d.delete()


def _reason(exc) -> str:
    if isinstance(exc, ClientError):
        return str(exc.response.get("Error", {}).get("Code") or "error")[:80]
    return type(exc).__name__


def _undo_add(d: MailDomain, *, created: bool) -> None:
    if created:  # only an identity this call created is ours to delete
        try:
            ses.delete_identity(d.name)
        except Exception:  # best effort: nothing may stop the row being removed
            log.warning("could not delete SES identity %s while undoing add", d.name, exc_info=True)
    d.delete()


def _cache_key(domain: MailDomain) -> str:
    return f"mail:dns:{domain.pk}:{domain.name}"


def records(domain: MailDomain) -> list:
    """The domain's DNS records with live status, reused for 60 s (a page load makes
    several lookups per record)."""
    key = _cache_key(domain)
    got = cache.get(key)
    if got is None:
        got = dns.check(domain)
        cache.set(key, got, _CHECK_TTL)
    return got


def refresh(domain: MailDomain, *, force: bool = False) -> MailDomain:
    now = timezone.now()
    if domain.checked_at:
        age = now - domain.checked_at
        if age < dt.timedelta(seconds=_FORCE_COOLDOWN) or (
                not force and age < dt.timedelta(seconds=_CHECK_TTL)):
            return domain
    if force:
        cache.delete(_cache_key(domain))
    if domain.status == MailDomain.PENDING:
        try:
            if ses.identity_verified(domain.name):
                # only flip a row that is still pending: a concurrent suspension wins
                if MailDomain.objects.filter(pk=domain.pk, status=MailDomain.PENDING).update(
                        status=MailDomain.VERIFIED):
                    domain.status = MailDomain.VERIFIED
        except ses.MailOff:
            pass
    mx = dns._resolve(domain.name, "MX")
    domain.mx_provider = dns.mx_provider(mx)
    domain.receiving = any(settings.MAIL_INBOUND_MX in h.lower() for h in mx)
    domain.checked_at = now
    domain.save(update_fields=["mx_provider", "receiving", "checked_at"])
    return domain


def remove_domain(domain: MailDomain) -> None:
    try:
        ses.delete_identity(domain.name)
    except ses.MailOff:
        pass
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "NotFoundException":
            log.error("SES identity %s not deleted (%s); removing the domain anyway",
                      domain.name, _reason(exc), exc_info=True)
    except BotoCoreError:
        log.error("SES identity %s not deleted; removing the domain anyway", domain.name, exc_info=True)
    cache.delete(_cache_key(domain))
    domain.delete()  # routes go with it; their threads and messages stay (route → NULL)


def save_route(domain: MailDomain, *, local_part: str, kind: str, agent_slug: str = "",
               forward_to_user_id: int | None = None, enabled: bool = True) -> MailRoute:
    local = (local_part or "").strip().lower()
    if len(local) > _LOCAL_MAX or not _LOCAL_RE.match(local):
        raise DomainError(422, "that is not a valid address before the @")
    if local == settings.MAIL_FORWARDER_LOCAL_PART:
        raise DomainError(422, f"{local}@ is reserved for forwarded mail")
    agent = forward_to = None
    if kind == MailRoute.AGENT:
        if local == MailRoute.CATCH_ALL:
            raise DomainError(422, "the catch-all can forward or drop, not belong to an agent")
        agent = Agent.objects.filter(slug=agent_slug, workspace=domain.workspace).first()
        if agent is None:
            raise DomainError(422, f"no agent {agent_slug!r} in this workspace")
    elif kind == MailRoute.FORWARD:
        forward_to = get_user_model().objects.filter(pk=forward_to_user_id).first()
        if forward_to is None or not wsvc.is_member(forward_to, domain.workspace_id):
            raise DomainError(422, "mail can only be forwarded to a member of this workspace")
    elif kind != MailRoute.DROP:
        raise DomainError(422, "kind must be agent, forward or drop")
    route, _ = MailRoute.objects.update_or_create(
        domain=domain, local_part=local,
        defaults={"kind": kind, "agent": agent, "forward_to": forward_to, "enabled": enabled})
    return route


def _domain_for(host: str) -> MailDomain | None:
    host = host.lower().rstrip(".")
    labels = host.split(".")
    candidates = [".".join(labels[i:]) for i in range(len(labels) - 1)]
    found = {d.name: d for d in MailDomain.objects.filter(name__in=candidates).select_related("workspace")}
    nearest = [found[c] for c in candidates if c in found]
    # a pending subdomain row must not shadow a live parent
    return next((d for d in nearest if d.routes_live), nearest[0] if nearest else None)


def resolve(address: str) -> tuple[MailDomain | None, MailRoute | None]:
    local, _, host = (address or "").strip().lower().rpartition("@")
    if not local or not host:
        return None, None
    domain = _domain_for(host)
    if domain is None:
        return None, None
    if not domain.routes_live or local == settings.MAIL_FORWARDER_LOCAL_PART:
        return domain, None
    routes = {r.local_part: r for r in domain.routes.filter(
        local_part__in=[local, MailRoute.CATCH_ALL], enabled=True).select_related("agent", "forward_to")}
    return domain, routes.get(local) or routes.get(MailRoute.CATCH_ALL)
