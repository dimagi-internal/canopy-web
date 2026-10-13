# apps/mail/domains.py
"""Domains and routes: what an admin configures, and the address → route answer."""
from __future__ import annotations

import datetime as dt
import re

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.agents.models import Agent
from apps.workspaces import services as wsvc

from . import dns, ses
from .models import MailDomain, MailRoute

_DOMAIN_RE = re.compile(r"^(?=.{1,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
_LOCAL_RE = re.compile(r"^(\*|[a-z0-9!#$%&'+/=?^_`{|}~-]+(\.[a-z0-9!#$%&'+/=?^_`{|}~-]+)*)$")


class DomainError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def add_domain(workspace, name: str, *, by) -> MailDomain:
    name = (name or "").strip().lower().rstrip(".")
    if not _DOMAIN_RE.match(name):
        raise DomainError(422, "that is not a domain name")
    if MailDomain.objects.filter(name=name).exists():
        raise DomainError(409, "that domain is already registered")
    try:
        with transaction.atomic():
            d = MailDomain.objects.create(workspace=workspace, name=name, created_by=by)
    except IntegrityError:
        raise DomainError(409, "that domain is already registered") from None
    try:
        d.dkim_tokens = ses.create_identity(name, mail_from=f"{d.mail_from_subdomain}.{name}")
    except ses.MailOff as exc:
        d.delete()
        raise DomainError(503, str(exc)) from None
    d.save(update_fields=["dkim_tokens"])
    return refresh(d, force=True)


def refresh(domain: MailDomain, *, force: bool = False) -> MailDomain:
    now = timezone.now()
    if not force and domain.checked_at and now - domain.checked_at < dt.timedelta(seconds=60):
        return domain
    if domain.status == MailDomain.PENDING:
        try:
            if ses.identity_verified(domain.name):
                domain.status = MailDomain.VERIFIED
        except ses.MailOff:
            pass
    mx = dns._resolve(domain.name, "MX")
    domain.mx_provider = dns.mx_provider(mx)
    domain.receiving = any(settings.MAIL_INBOUND_MX in h.lower() for h in mx)
    domain.checked_at = now
    domain.save(update_fields=["status", "mx_provider", "receiving", "checked_at"])
    return domain


def remove_domain(domain: MailDomain) -> None:
    try:
        ses.delete_identity(domain.name)
    except ses.MailOff:
        pass
    domain.routes.update(enabled=False)
    domain.delete()


def save_route(domain: MailDomain, *, local_part: str, kind: str, agent_slug: str = "",
               forward_to_user_id: int | None = None, enabled: bool = True) -> MailRoute:
    local = (local_part or "").strip().lower()
    if not _LOCAL_RE.match(local):
        raise DomainError(422, "that is not a valid address before the @")
    if local == settings.MAIL_FORWARDER_LOCAL_PART:
        raise DomainError(422, f"{local}@ is reserved for forwarded mail")
    agent = forward_to = None
    if kind == MailRoute.AGENT:
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
    return next((found[c] for c in candidates if c in found), None)


def resolve(address: str) -> tuple[MailDomain | None, MailRoute | None]:
    local, _, host = (address or "").strip().lower().rpartition("@")
    if not local or not host:
        return None, None
    domain = _domain_for(host)
    if domain is None:
        return None, None
    if not domain.routes_live:
        return domain, None
    routes = {r.local_part: r for r in domain.routes.filter(
        local_part__in=[local, MailRoute.CATCH_ALL], enabled=True).select_related("agent", "forward_to")}
    return domain, routes.get(local) or routes.get(MailRoute.CATCH_ALL)
