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
from .models import MailDomain, MailDomainShare, MailRoute

_DOMAIN_RE = re.compile(
    r"^(?=.{1,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+([a-z]{2,63}|xn--[a-z0-9-]{1,59})$")
_LOCAL_RE = re.compile(r"^(\*|[a-z0-9!#$%&'+/=?^_`{|}~-]+(\.[a-z0-9!#$%&'+/=?^_`{|}~-]+)*)$")
_LOCAL_MAX = 64
# Addresses only the domain's own workspace may route: the catch-all, the DMARC report
# address, and the role addresses (RFC 2142) a mail provider or registrar writes to.
_OWNER_ONLY = {MailRoute.CATCH_ALL, "postmaster", "abuse", "hostmaster", "webmaster", "security"}


log = logging.getLogger(__name__)

_CHECK_TTL = 60  # seconds a DNS check is reused
_FORCE_COOLDOWN = 10  # seconds between forced refreshes of one domain


class DomainError(Exception):
    def __init__(self, status: int, message: str, *, keep: bool = False):
        super().__init__(message)
        self.status = status
        self.keep = keep
        """True when the refusal changed state that must be committed (answer, don't raise)."""


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
        released = _release(holder)
    else:
        released = False
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
        d = refresh(d, force=True)
        d.released_stale_claim = released  # for the add's audit note; not a field
        return d
    except Exception as exc:
        _undo_add(d, created=created)
        if isinstance(exc, ses.MailOff):
            raise DomainError(503, str(exc)) from None
        if isinstance(exc, ses.IdentityExists):
            raise DomainError(409, "that domain is already registered with the mail provider") from None
        if isinstance(exc, (ClientError, BotoCoreError)):
            raise DomainError(502, f"SES refused: {_reason(exc)}") from None
        raise


def _ttl_cutoff():
    return timezone.now() - dt.timedelta(days=settings.MAIL_PENDING_DOMAIN_TTL_DAYS)


def _stale_pending(d: MailDomain) -> bool:
    """A domain still pending DNS after the TTL is abandoned: it never proved control, so
    it may not hold the name against anyone (in any workspace) any longer."""
    return d.status == MailDomain.PENDING and d.created_at < _ttl_cutoff()


def _not_found(exc) -> bool:
    return isinstance(exc, ClientError) and exc.response.get("Error", {}).get("Code") == "NotFoundException"


def _release(holder: MailDomain) -> bool:
    """Remove an abandoned pending domain — but only if SES agrees it never verified.

    Under a row lock: ask SES first (the row may be stale while DNS did verify — then the
    row is flipped to verified and the name stays taken, 409). Otherwise delete the row
    (and its routes; threads stay, route → NULL) conditionally on it still being pending
    past the TTL, and only when that delete removed it delete its SES identity (canopy
    created it for that row). Any provider failure keeps the row: 503 mail off, 502 else.
    """
    with transaction.atomic():
        row = MailDomain.objects.select_for_update(of=("self",)).filter(pk=holder.pk).first()
        if row is None:
            return False  # released concurrently: the name is free, nothing was ours to release
        try:
            verified = ses.identity_verified(row.name)
        except ses.MailOff as exc:
            raise DomainError(503, str(exc)) from None
        except (ClientError, BotoCoreError) as exc:
            if not _not_found(exc):
                raise DomainError(502, f"the mail provider refused: {_reason(exc)}") from None
            verified = False
        if not verified:
            _release_locked(row)
            released = True
        else:
            # SES says DKIM verified: the row was only stale. It keeps the name.
            MailDomain.objects.filter(pk=row.pk, status=MailDomain.PENDING).update(status=MailDomain.VERIFIED)
            released = False
    if not released:
        raise DomainError(409, "that domain is already registered", keep=True)
    cache.delete(_cache_key(row))
    # The holder side is logged server-side only: the adder's audit row says a stale claim
    # was released and nothing about whose it was (no cross-tenant detail either way).
    log.info("mail: released stale pending domain %s (held by workspace %s, created %s)",
             row.name, row.workspace_id, row.created_at.isoformat())
    return True


def _release_locked(row: MailDomain) -> None:
    _, per_model = MailDomain.objects.filter(
        pk=row.pk, status=MailDomain.PENDING, created_at__lt=_ttl_cutoff()).delete()
    if per_model.get(MailDomain._meta.label, 0) != 1:
        raise DomainError(409, "that domain is already registered")
    try:
        ses.delete_identity(row.name)
    except ses.MailOff as exc:
        raise DomainError(503, str(exc)) from None  # raised inside the lock's transaction: the delete rolls back
    except (ClientError, BotoCoreError) as exc:
        if not _not_found(exc):
            raise DomainError(502, f"the mail provider refused: {_reason(exc)}") from None


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


def share_for(domain: MailDomain, workspace) -> MailDomainShare | None:
    """The grant that lets `workspace` use `domain`: one on the workspace itself, else on
    its nearest ancestor. None for the domain's own workspace and for anyone not covered."""
    if domain.workspace_id == workspace.pk:
        return None
    chain = [workspace.pk, *workspace.ancestor_slugs()]
    by_ws = {s.workspace_id: s for s in domain.shares.filter(workspace_id__in=chain)}
    return next((by_ws[w] for w in chain if w in by_ws), None)


def may_use(domain: MailDomain, workspace) -> bool:
    return domain.workspace_id == workspace.pk or share_for(domain, workspace) is not None


def usable_domains(workspace) -> list[MailDomain]:
    """The workspace's own domains, then the domains shared with it (or an ancestor)."""
    chain = [workspace.pk, *workspace.ancestor_slugs()]
    own = list(workspace.mail_domains.all())
    shared = (MailDomain.objects.filter(shares__workspace_id__in=chain)
              .exclude(workspace=workspace).select_related("workspace").distinct())
    return own + list(shared)


def route_is_live(route: MailRoute) -> bool:
    """A route on a shared domain stops working the moment its workspace loses the share."""
    return route.workspace_id == route.domain.workspace_id or share_for(route.domain, route.workspace) is not None


def save_route(domain: MailDomain, *, local_part: str, kind: str, agent_slug: str = "",
               forward_to_user_id: int | None = None, enabled: bool = True,
               workspace=None) -> MailRoute:
    """Create or replace one address's route, held by `workspace` (default: the domain's).

    On a shared domain the local part is first come, first served across every workspace
    using it: an address another workspace holds is 409 and never replaced or named."""
    workspace = workspace or domain.workspace
    owner = workspace.pk == domain.workspace_id
    if not owner and not may_use(domain, workspace):
        raise DomainError(404, "no such domain")
    local = (local_part or "").strip().lower()
    if len(local) > _LOCAL_MAX or not _LOCAL_RE.match(local):
        raise DomainError(422, "that is not a valid address before the @")
    if local == settings.MAIL_FORWARDER_LOCAL_PART:
        raise DomainError(422, f"{local}@ is reserved for forwarded mail")
    if not owner and (local in _OWNER_ONLY or local == settings.MAIL_DMARC_LOCAL_PART):
        raise DomainError(422, f"{local}@ on a shared domain is kept for the domain's own workspace")
    held = MailRoute.objects.filter(domain=domain, local_part=local).first()
    if held is not None and held.workspace_id != workspace.pk:
        raise DomainError(409, "that address is already taken")
    agent = forward_to = None
    if kind == MailRoute.AGENT:
        if local == MailRoute.CATCH_ALL:
            raise DomainError(422, "the catch-all can forward or drop, not belong to an agent")
        agent = Agent.objects.filter(slug=agent_slug, workspace=workspace).first()
        if agent is None:
            raise DomainError(422, f"no agent {agent_slug!r} in this workspace")
    elif kind == MailRoute.FORWARD:
        forward_to = get_user_model().objects.filter(pk=forward_to_user_id).first()
        if forward_to is None or not wsvc.is_member(forward_to, workspace.pk):
            raise DomainError(422, "mail can only be forwarded to a member of this workspace")
    elif kind != MailRoute.DROP:
        raise DomainError(422, "kind must be agent, forward or drop")
    try:
        with transaction.atomic():
            route, _ = MailRoute.objects.update_or_create(
                domain=domain, local_part=local, workspace=workspace,
                defaults={"kind": kind, "agent": agent, "forward_to": forward_to, "enabled": enabled})
    except IntegrityError:  # another workspace took it between the check and the write
        raise DomainError(409, "that address is already taken") from None
    return route


def hosted_domain_for(address: str, workspace) -> MailDomain | None:
    """The workspace's canopy-hosted domain `address` is on (its own, or one shared with
    it), or None — an address elsewhere (Gmail, Google Workspace mail) is not ours to route."""
    _, _, host = (address or "").strip().lower().rpartition("@")
    if not host:
        return None
    return next((d for d in usable_domains(workspace) if d.name == host), None)


def agent_mailbox(agent) -> dict:
    """Where `agent.email` lives, for every surface that would otherwise assume Gmail:
    `{"kind": "hosted", "address", "domain", "live", "route"}` when it is a canopy-hosted
    address (live = the agent route exists, is enabled and its domain is verified),
    `{"kind": "google", "address"}` for anything else, `{"kind": "none"}` with no email."""
    if not agent.email:
        return {"kind": "none"}
    address = agent.email.strip().lower()
    domain = hosted_domain_for(address, agent.workspace)
    if domain is None:
        return {"kind": "google", "address": address}
    local = address.rpartition("@")[0]
    route = MailRoute.objects.filter(domain=domain, local_part=local).select_related("agent").first()
    mine = route is not None and route.kind == MailRoute.AGENT and route.agent_id == agent.pk
    return {"kind": "hosted", "address": address, "domain": domain.name,
            "verified": domain.routes_live,
            "live": bool(mine and route.enabled and domain.routes_live and route_is_live(route)),
            "route": "mine" if mine else ("taken" if route is not None else "none")}


def agent_addresses(agent) -> list[dict]:
    """Every address `agent` has, `[{"address", "backend": "ses"|"gmail", "primary"}]`:
    its own (`Agent.email`, primary), then its enabled agent routes, then its Gmail push
    mailboxes — lowercase, each once. An agent may hold several (ada@dimagi-ai.com in
    Gmail and ada@dimagi.ai here); which one is its Google login is `google_identity`."""
    from apps.inbound.models import InboundMailbox

    out: dict[str, dict] = {}

    def add(address: str, backend: str, primary: bool = False) -> None:
        a = (address or "").strip().lower()
        if a and a not in out:
            out[a] = {"address": a, "backend": backend, "primary": primary}

    if agent.email:
        add(agent.email, "ses" if agent_mailbox(agent)["kind"] == "hosted" else "gmail", True)
    routes = (MailRoute.objects.filter(agent=agent, kind=MailRoute.AGENT, enabled=True,
                                       workspace=agent.workspace_id)
              .exclude(local_part=MailRoute.CATCH_ALL).select_related("domain")
              .order_by("domain__name", "local_part"))
    for r in routes:
        add(r.address, "ses")
    for a in InboundMailbox.objects.filter(agent=agent).order_by("address").values_list("address", flat=True):
        add(a, "gmail")
    return list(out.values())


def ensure_agent_route(agent, *, by) -> str:
    """Give an agent whose address is on a canopy-hosted domain its mailbox route, so mail to
    it becomes its email turn with no further step. Returns what happened: "created",
    "exists", "taken" (that address already routes somewhere else — never replaced),
    "needs-admin", "not-hosted", or "none". Safe to call on every save; never raises.

    `by` must be able to change the workspace's mail routes (the same INTEGRATIONS gate
    as the email page): an unrouted address falls to the catch-all, so letting anyone who
    can register an agent claim one would let an editor read mail meant for someone else."""
    from apps.workspaces import permissions as perms

    box = agent_mailbox(agent)
    if box["kind"] != "hosted":
        return "none" if box["kind"] == "none" else "not-hosted"
    if box["route"] == "mine":
        return "exists"
    if box["route"] == "taken":
        return "taken"
    if by is None or not perms.can(by, agent.workspace, perms.INTEGRATIONS):
        return "needs-admin"
    domain = hosted_domain_for(box["address"], agent.workspace)
    try:
        save_route(domain, local_part=box["address"].rpartition("@")[0], kind=MailRoute.AGENT,
                   agent_slug=agent.slug, workspace=agent.workspace)
    except DomainError as exc:
        log.info("no mailbox route for %s: %s", agent.slug, exc)
        return "taken"
    return "created"


class MailboxProvider:
    """`apps.agents.mailbox`'s provider: hosted mail is this app's."""

    def describe(self, agent) -> dict:
        return agent_mailbox(agent)

    def ensure_route(self, agent, *, by) -> str:
        return ensure_agent_route(agent, by=by)

    def addresses(self, agent) -> list[dict]:
        return agent_addresses(agent)

    def reply(self, agent, *, thread_id: str, to: str, subject: str = "", body: str) -> str:
        """Reply on one of `agent`'s canopy-hosted threads from the thread's own address
        (`agent_mail.send`). Returns the sent Message-ID; raises `mailbox.ReplyRefused`
        when nothing was sent."""
        import uuid as uuidlib

        from apps.agents import mailbox

        from . import agent_mail
        from .models import MailThread

        try:
            key = uuidlib.UUID(str(thread_id))
        except ValueError:
            raise mailbox.ReplyRefused("not a canopy-hosted thread") from None
        t = (MailThread.objects.filter(uuid=key, workspace=agent.workspace_id)
             .select_related("route__agent", "route__domain", "route__workspace").first())
        if t is None:
            raise mailbox.ReplyRefused("no such thread")
        r = t.route
        if r is None or not r.enabled or r.kind != MailRoute.AGENT or r.agent_id != agent.pk:
            raise mailbox.ReplyRefused("this address no longer routes to the agent")
        try:
            m = agent_mail.send(r, to=[to], text=body, subject=subject, thread=t)
        except agent_mail.SendRefused as exc:
            raise mailbox.ReplyRefused(str(exc)) from None
        return m.message_id


def verified_domain_owner(domain: str):
    """The workspace whose VERIFIED mail domain `domain` is, or None (sign-in seam)."""
    d = MailDomain.objects.filter(name=domain.lower()).select_related("workspace").first()
    return d.workspace if d is not None and d.routes_live else None


def set_share(domain: MailDomain, workspace, *, daily_send_cap: int | None = None, by=None) -> MailDomainShare:
    # A grant on the domain's own workspace is how its sub-workspaces get it: the owner
    # needs no grant itself (share_for answers None for it), its descendants find this one.
    if daily_send_cap is not None and not 1 <= daily_send_cap <= 5000:
        raise DomainError(422, "daily_send_cap must be between 1 and 5000")
    share, created = MailDomainShare.objects.get_or_create(
        domain=domain, workspace=workspace,
        defaults={"daily_send_cap": daily_send_cap or 200, "created_by": by})
    if not created and daily_send_cap is not None and share.daily_send_cap != daily_send_cap:
        share.daily_send_cap = daily_send_cap
        share.save(update_fields=["daily_send_cap"])
    return share


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
        local_part__in=[local, MailRoute.CATCH_ALL], enabled=True).select_related(
            "agent", "forward_to", "workspace", "domain")}
    exact = routes.get(local)
    if exact is not None and not route_is_live(exact):
        exact = None  # its workspace lost the share: the address falls to the catch-all
    if local == settings.MAIL_DMARC_LOCAL_PART:
        # DMARC aggregate reports: never swept up by the catch-all and forwarded to a person.
        return domain, exact
    return domain, exact or routes.get(MailRoute.CATCH_ALL)
