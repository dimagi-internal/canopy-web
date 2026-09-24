"""Registering a website that may host canopy's widget, as an ordinary product act.

**Why this exists.** Every fact the widget needs — which app, which URLs may
frame it, which agents it may offer — was reachable only through the Django
admin. That is the wrong door twice over. Operationally it is staff-only and
looks nothing like the rest of canopy, so the person who wants to embed an
agent cannot do it; and conceptually "connect my site to canopy" is a thing a
user does, not a database row an administrator edits.

**Who administers an app.** Its owning workspace's owners, matching every other
tenant-admin surface here (members, invites, the shared vault). Registration is
therefore an act inside a tenant rather than a global one, which is what makes
"who may change this later" answerable at all — `created_by` alone would strand
an app the moment that person moved on.

**What the surface deliberately will not do.** It does not let a site speak for
canopy's users by email domain. A site vouches for a visitor with a signed
assertion, and canopy decides who that is: an account they already have at one
of `resolvable_domains` (a grant bounded to the setting owner's own domain), or
a contact — never a new account, and never a membership. The provisioning grant
this paragraph used to describe went with `/api/auth/token-exchange`
(2026-09-22), so an app registered here can only ever be embedded.
"""

from __future__ import annotations

from django.db import transaction
from django.db.models import QuerySet
from django.utils import timezone

from apps.workspaces import services as wsvc
from apps.workspaces.models import WorkspaceMembership

from .models import AppCredential, AppCredentialAgent, is_valid_frame_origin


class EmbedAppError(Exception):
    """A refusal with a reason a person can act on.

    `code` is a closed set so the API can map it to a status without matching
    on prose: `not_found`, `not_owner`, `bad_origin`, `bad_domain`,
    `unknown_agent`, `duplicate_name`, `bad_name`.
    """

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def owned_workspace_slugs(user) -> set[str]:
    """Workspaces this user OWNS. Editors and viewers are not administrators."""
    return set(
        WorkspaceMembership.objects.filter(
            user=user, role=WorkspaceMembership.OWNER
        ).values_list("workspace_id", flat=True)
    )


def require_owner(user, slug: str) -> None:
    """404 for a non-member, 403 for a member who is not an owner.

    The order matters and is the same one `apps/workspaces/api.py::_require_role`
    uses: answering 403 to someone with no membership would confirm the
    workspace exists, so a stranger could enumerate tenants by probing slugs.
    """
    if not wsvc.is_member(user, slug):
        raise EmbedAppError("not_found", f"workspace {slug!r} not found")
    if slug not in owned_workspace_slugs(user):
        raise EmbedAppError("not_owner", "only a workspace owner can manage connected apps")


def apps_for(user, slug: str) -> QuerySet[AppCredential]:
    """The sites this workspace has connected — its own rows, and only those.

    Each tenant registers a system itself, so there is no "site another
    workspace registered that we also use": what is listed here is exactly
    what an owner here may change, all of it. Retired rows are left out; a
    disconnected site is gone from the tenant's point of view.
    """
    require_owner(user, slug)
    return (
        AppCredential.objects
        .filter(workspace_id=slug, revoked_at__isnull=True)
        .prefetch_related("allowed_agents__agent")
        .order_by("name")
    )


def resolve_site(name: str, agent_slug: str = "") -> AppCredential | None:
    """Which tenant's registration a host means by `name` — the ONE place a
    site name becomes a row.

    A name is only unique within a tenant, so it needs a tenant beside it, and
    the tenant comes from the AGENT the host names: an agent belongs to exactly
    one workspace, and a host already knows which agent it is mounting. Always
    the agent's own workspace's row — never another tenant's row of the same
    name, whatever that row lists.

    With no agent named, the name must be unambiguous among live sites. That is
    what every integration written before per-tenant sites sends, and it keeps
    working exactly until a second tenant registers the same name — at which
    point this refuses (`AmbiguousSite`) rather than pick one, because picking
    would route a visitor into a tenant their host never meant. The fix the
    refusal names is to send the agent, which removes the dependency on any
    other tenant's choices entirely.

    Returns None when nothing matches. Callers answer "unknown" identically for
    a missing agent and a missing site, so a prober learns nothing about
    another tenant's agents or registrations.
    """
    name = (name or "").strip()
    if not name:
        return None
    live = AppCredential.objects.filter(name=name, revoked_at__isnull=True)
    slug = (agent_slug or "").strip()
    if slug:
        from apps.agents.models import Agent

        workspace_id = (Agent.objects.filter(slug=slug)
                        .values_list("workspace_id", flat=True).first())
        if workspace_id is None:
            return None
        return live.filter(workspace_id=workspace_id).first()
    rows = list(live[:2])
    if len(rows) > 1:
        raise AmbiguousSite(name)
    return rows[0] if rows else None


class AmbiguousSite(Exception):
    """More than one tenant has a live site by this name and the caller did not
    say which. Carries the fix, because the person who hits it is an
    integrator looking at a host that worked yesterday."""

    def __init__(self, name: str):
        self.name = name
        super().__init__(
            f"more than one workspace has a site named {name!r}; name the agent "
            "(`agent_slug`, or `agent` in canopy.init) so canopy knows which one"
        )


def _clean_origins(origins) -> list[str]:
    """Validate the URLs, and say which one is wrong.

    `frame_origins()` filters on read, so a bad value can never produce a
    permissive header — but it would silently not apply, and someone who typed
    it would believe their site was connected. The failure they would actually
    see is the widget never appearing, with nothing to link it back to a typo.
    """
    if not isinstance(origins, list):
        raise EmbedAppError("bad_origin", "expected a list of site URLs")
    cleaned: list[str] = []
    for raw in origins:
        value = (raw or "").strip().rstrip("/") if isinstance(raw, str) else raw
        if not value:
            continue
        if not is_valid_frame_origin(value):
            raise EmbedAppError(
                "bad_origin",
                f"{raw!r} is not a site URL canopy can use. Give scheme, host and "
                "optional port only — https://labs.connect.dimagi.com or "
                "http://localhost:8000 — with no path and no wildcard. A wildcard "
                "is refused on purpose: this list is what stops any other site "
                "framing your agent.",
            )
        if value not in cleaned:
            cleaned.append(value)
    return cleaned


def _clean_resolvable(user, domains) -> list[str]:
    """Domains this site may resolve to existing canopy users — bounded twice.

    A site allowed to resolve a domain can assert any verified address in it,
    so a compromised site key could speak for EXISTING canopy users there
    (never create one). So the grant is narrow by construction: only a domain
    the person setting it is in (their own verified login address), and only
    one canopy itself admits at login. Anything else is refused with the reason.
    """
    from apps.common.auth_domains import allowed_email_domains

    if not domains:
        return []
    admitted = {d.lower() for d in allowed_email_domains()}
    own = (getattr(user, "email", "") or "").lower().rpartition("@")[2]
    cleaned: list[str] = []
    for raw in domains:
        d = str(raw or "").strip().lower().lstrip("@")
        if not d:
            continue
        if d not in admitted:
            raise EmbedAppError("domain_not_admitted",
                                f"{d} is not a domain canopy admits at login")
        if d != own:
            raise EmbedAppError("domain_not_yours",
                                f"you can only let a site resolve your own domain ({own or 'none'}), "
                                f"not {d}")
        if d not in cleaned:
            cleaned.append(d)
    return cleaned


def _clean_keys(keys) -> list[str]:
    """Accept only PEM PUBLIC keys, and say so when something else is pasted.

    Two mistakes are worth catching at the door rather than at the next
    assertion. Pasting a PRIVATE key is the dangerous one — it would work, and
    the site's signing key would then be sitting in canopy's database, undoing
    the entire reason for using signatures. Pasting something unparseable is
    merely useless, but it fails at a moment far from the paste: assertions
    stop verifying and nothing points back here.
    """
    from cryptography.hazmat.primitives import serialization

    if not isinstance(keys, list):
        raise EmbedAppError("bad_key", "expected a list of PEM public keys")
    cleaned: list[str] = []
    for raw in keys:
        pem = (raw or "").strip() if isinstance(raw, str) else ""
        if not pem:
            continue
        if "PRIVATE KEY" in pem:
            raise EmbedAppError(
                "bad_key",
                "that is a PRIVATE key. Keep it on your own server and paste the "
                "PUBLIC half here — canopy holding your signing key would undo "
                "the whole point of signing.",
            )
        try:
            serialization.load_pem_public_key(pem.encode())
        except Exception as exc:  # noqa: BLE001
            raise EmbedAppError(
                "bad_key",
                f"could not read that as a PEM public key ({exc}). It should start "
                "with -----BEGIN PUBLIC KEY-----.",
            ) from exc
        if pem not in cleaned:
            cleaned.append(pem)
    return cleaned


def set_resolvable(*, user, app: AppCredential, domains) -> None:
    """Replace the domains this site may resolve to existing canopy users.

    Only what is being ADDED is bounded to the setter's own domain: removing
    one must never require the remover to be in it.
    """
    current = set(app.resolvable_domains or [])
    wanted = [str(d or "").strip().lower().lstrip("@") for d in domains]
    _clean_resolvable(user, [d for d in wanted if d and d not in current])
    app.resolvable_domains = [d for d in dict.fromkeys(wanted) if d]
    app.save(update_fields=["resolvable_domains"])


def set_agents(app: AppCredential, slugs: list[str]) -> None:
    """Replace the agents this site may offer.

    Only agents in the site's own workspace. The picker intersects the
    allowlist with the viewer's memberships, so a foreign agent here would be
    invisible to everyone and read as a grant that silently does nothing —
    worse than a refusal. Another tenant offering its agent through the same
    external system registers that system itself.
    """
    from apps.agents.models import Agent

    wanted = [s for s in dict.fromkeys(slugs or []) if s]
    found = {
        a.slug: a
        for a in Agent.objects.filter(slug__in=wanted, workspace_id=app.workspace_id)
    }
    missing = [s for s in wanted if s not in found]
    if missing:
        raise EmbedAppError(
            "unknown_agent",
            f"not agents in this workspace: {', '.join(missing)}. A site can only "
            "offer agents belonging to the workspace that connected it.",
        )
    AppCredentialAgent.objects.filter(app=app).exclude(agent__slug__in=wanted).delete()
    for slug in wanted:
        AppCredentialAgent.objects.get_or_create(app=app, agent=found[slug])


def _clean_jwks_url(url) -> str:
    """A site's published-keys URL, checked before it is stored.

    Checked HERE rather than only at fetch time so the person who typed it sees
    the reason — a staging URL inside a VPC, or a plain-http one — instead of
    assertions quietly failing later for a reason nothing points back here.
    """
    from . import jwks as jwks_mod

    try:
        return jwks_mod.validate_url(url if isinstance(url, str) else "")
    except jwks_mod.JwksError as exc:
        raise EmbedAppError("bad_jwks_url", str(exc)) from exc


@transaction.atomic
def register(*, user, workspace_slug: str, name: str, origins: list[str],
             agents: list[str] | None = None,
             public_keys: list[str] | None = None,
             jwks_url: str | None = None,
             resolvable_domains: list[str] | None = None,
             ) -> AppCredential:
    """Register a site in this workspace. It holds no secret: the site proves
    itself by signing, against the keys at `jwks_url` (or pasted)."""
    require_owner(user, workspace_slug)

    name = (name or "").strip()
    if not name:
        raise EmbedAppError("bad_name", "give the app a name")
    # The name travels in a URL (`/embed/chat?app=…`) and is what the host
    # passes to `canopy.init`, so it is an identifier rather than a label.
    if not all(c.isalnum() or c in "-_" for c in name):
        raise EmbedAppError(
            "bad_name",
            f"{name!r} cannot be used as a name — letters, digits, hyphens and "
            "underscores only. It appears in a URL and in the host's own code.",
        )
    # Per tenant, not canopy-wide: another workspace's `connect-labs` is its
    # own business, and refusing a name because a stranger holds it would be
    # the cross-tenant coupling this model exists to remove.
    if AppCredential.objects.filter(name=name, workspace_id=workspace_slug,
                                    revoked_at__isnull=True).exists():
        raise EmbedAppError("duplicate_name",
                            f"this workspace already has a site named {name!r}")

    cleaned_origins = _clean_origins(origins)
    cleaned_keys = _clean_keys(public_keys or [])
    cleaned_jwks = _clean_jwks_url(jwks_url or "")
    # Validated BEFORE the row exists, so a refusal leaves nothing half-made.
    cleaned_domains = _clean_resolvable(user, resolvable_domains or [])

    app = AppCredential.create_credential(name=name, created_by=user,
                                               workspace=workspace_slug)
    app.allowed_frame_origins = cleaned_origins
    app.public_keys = cleaned_keys
    app.jwks_url = cleaned_jwks
    app.resolvable_domains = cleaned_domains
    app.save(update_fields=["allowed_frame_origins", "public_keys", "jwks_url",
                            "resolvable_domains"])
    set_agents(app, agents or [])
    return app


def update(*, user, app: AppCredential, workspace_slug: str, origins=None, agents=None,
           public_keys=None, resolvable_domains=None, jwks_url=None) -> AppCredential:
    """Change what this workspace's site may do. Every field is this tenant's
    own, so there is one gate — owning the workspace the row belongs to."""
    require_owner(user, workspace_slug)
    if app.workspace_id != workspace_slug:
        raise EmbedAppError("not_found", "no such connected site in this workspace")

    fields: list[str] = []
    if origins is not None:
        app.allowed_frame_origins = _clean_origins(origins)
        fields.append("allowed_frame_origins")
    if public_keys is not None:
        app.public_keys = _clean_keys(public_keys)
        fields.append("public_keys")
    if jwks_url is not None:
        app.jwks_url = _clean_jwks_url(jwks_url)
        fields.append("jwks_url")
    if fields:
        app.save(update_fields=fields)
    if resolvable_domains is not None:
        set_resolvable(user=user, app=app, domains=resolvable_domains)
    if agents is not None:
        set_agents(app, agents)
    return app


def revoke(app: AppCredential) -> AppCredential:
    """Retire this registration. Its embed shell 404s immediately and
    `resolve_site` stops finding it, so nothing it signs verifies.

    Revoked rather than deleted: the row is what this tenant's contacts and
    tokens hang off, and it is the audit trail of what the tenant once allowed.
    """
    if app.revoked_at is None:
        app.revoked_at = timezone.now()
        app.save(update_fields=["revoked_at"])
    return app


def disconnect(*, user, app: AppCredential, workspace_slug: str) -> AppCredential:
    """This tenant stops using the site.

    Just `revoke`, now that the row is this tenant's alone. It used to carry
    custody transfer and a last-grant check, because the row was shared and
    retiring it would have ended every other tenant's integration; another
    tenant using the same external system has its own row and does not notice.
    """
    require_owner(user, workspace_slug)
    if app.workspace_id != workspace_slug:
        raise EmbedAppError("not_found", "no such connected site in this workspace")
    return revoke(app)


def self_app():
    """The app whose widget canopy shows on its own pages, or None.

    One query on a column, where this used to be a name read from a setting
    and looked up. Nothing special-cases canopy: it is whichever connected
    site an owner ticked the box on, and usually that site is canopy itself.
    """
    return AppCredential.objects.filter(
        show_on_canopy_pages=True, revoked_at__isnull=True
    ).order_by("name").first()


def set_show_on_canopy_pages(*, app: AppCredential, on: bool, origin: str) -> AppCredential:
    """Turn canopy's own panel on or off for this app.

    Ticking it also ensures canopy's own origin is in the app's URL list,
    because the two are not independent: `frame-ancestors` is built from that
    list, so a ticked app without it produces a shell that 404s — on by
    every visible measure and dead in the browser. The origin comes from the
    request rather than the form for the same reason it does everywhere else:
    it is the address the person is looking at, and the one value that cannot
    be typed wrong.
    """
    fields = ["show_on_canopy_pages"]
    if on:
        other = (
            AppCredential.objects.filter(show_on_canopy_pages=True, revoked_at__isnull=True)
            .exclude(pk=app.pk)
            .first()
        )
        if other is not None:
            raise EmbedAppError(
                "already_shown",
                f"{other.name!r} already shows its panel on canopy's pages. Turn "
                "that one off first — canopy can only show one.",
            )
        wanted = _clean_origins([origin])
        if wanted and wanted[0] not in (app.allowed_frame_origins or []):
            app.allowed_frame_origins = [*(app.allowed_frame_origins or []), wanted[0]]
            fields.append("allowed_frame_origins")
    app.show_on_canopy_pages = bool(on)
    app.save(update_fields=fields)
    return app
