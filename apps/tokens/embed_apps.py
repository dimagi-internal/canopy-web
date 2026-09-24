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
    """The sites this workspace has anything to say about.

    Two kinds, deliberately in one list: sites this workspace REGISTERED, and
    sites another workspace registered that THIS one has granted. Both are
    things an owner here can act on — what they may change differs, and
    `update` is what enforces that — and splitting them would make "which sites
    act for us" a question with two answers.

    Membership of the grant table, never `workspace_id == slug`, so a site
    predating this surface (no owning workspace, so no grant) is excluded by
    construction rather than by remembering to exclude it. A nullable tenant FK
    read as "no tenant ⇒ allowed" is the exact bug ARCHITECTURE.md records
    against `Agent.workspace`.
    """
    require_owner(user, slug)
    return (
        AppCredential.objects
        .filter(tenant_grants__workspace_id__in={slug} & owned_workspace_slugs(user))
        .distinct()
        .prefetch_related("allowed_agents__agent", "tenant_grants")
        .order_by("name")
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


def tenant_grant(app: AppCredential, workspace_slug: str):
    """This tenant's grant to this site, or None. The one place that question
    is asked, so "has this tenant authorized this site" has a single answer."""
    from .models import AppCredentialTenant

    return AppCredentialTenant.objects.filter(app=app, workspace_id=workspace_slug).first()


def authorize_tenant(*, user, app: AppCredential, workspace_slug: str,
                     resolvable_domains=None):
    """An owner of THIS workspace lets this site act for it.

    The grant a site needs before it can offer any of this tenant's agents or
    record a visitor here. Made per tenant, by that tenant's own owner, because
    a site serving several tenants is several separate decisions — and one
    tenant's owner has no standing to make another's.
    """
    from .models import AppCredentialTenant

    require_owner(user, workspace_slug)
    grant, _created = AppCredentialTenant.objects.get_or_create(
        app=app, workspace_id=workspace_slug, defaults={"created_by": user},
    )
    if resolvable_domains is not None:
        current = set(grant.resolvable_domains or [])
        wanted = [str(d or "").strip().lower().lstrip("@") for d in resolvable_domains]
        # Only what is being ADDED is bounded to the setter's own domain:
        # removing one must never require the remover to be in it.
        _clean_resolvable(user, [d for d in wanted if d and d not in current])
        grant.resolvable_domains = [d for d in dict.fromkeys(wanted) if d]
        grant.save(update_fields=["resolvable_domains"])
    return grant


def revoke_tenant(*, user, app: AppCredential, workspace_slug: str) -> None:
    """This tenant withdraws. Its agents stop being offered and no visitor is
    recorded here again; the site's identity and every other tenant's grant are
    untouched, which is the whole point of the grant being a row."""
    from apps.agents.models import Agent

    require_owner(user, workspace_slug)
    AppCredentialAgent.objects.filter(
        app=app, agent__in=Agent.objects.filter(workspace_id=workspace_slug),
    ).delete()
    grant = tenant_grant(app, workspace_slug)
    if grant is not None:
        grant.delete()


def set_agents(app: AppCredential, workspace_slug: str, slugs: list[str]) -> None:
    """Replace the agents THIS TENANT offers through this site.

    Scoped to one workspace in both directions, and the delete half is the part
    that matters: replacing every row for the site would let one tenant's owner
    silently withdraw another tenant's agents, which is exactly the authority
    this model exists to keep separate.

    Only agents in this workspace. The picker already intersects the allowlist
    with the viewer's memberships, so a foreign agent here would be invisible to
    everyone and read as a grant that silently does nothing — worse than a
    refusal. Offering another tenant's agent is that tenant's call to make.
    """
    from apps.agents.models import Agent

    if tenant_grant(app, workspace_slug) is None:
        raise EmbedAppError(
            "not_granted",
            f"{app.name!r} is not authorized to act for this workspace yet",
        )
    wanted = [s for s in dict.fromkeys(slugs or []) if s]
    found = {
        a.slug: a
        for a in Agent.objects.filter(slug__in=wanted, workspace_id=workspace_slug)
    }
    missing = [s for s in wanted if s not in found]
    if missing:
        raise EmbedAppError(
            "unknown_agent",
            f"not agents in this workspace: {', '.join(missing)}. A site can only "
            "offer agents belonging to the workspace granting them.",
        )
    AppCredentialAgent.objects.filter(
        app=app, agent__in=Agent.objects.filter(workspace_id=workspace_slug),
    ).exclude(agent__slug__in=wanted).delete()
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


def register(*, user, workspace_slug: str, name: str, origins: list[str],
             agents: list[str] | None = None,
             public_keys: list[str] | None = None,
             jwks_url: str | None = None,
             resolvable_domains: list[str] | None = None,
             ) -> tuple[str, AppCredential]:
    """Register an app and return `(raw secret, row)`. The secret is shown once."""
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
    if AppCredential.objects.filter(name=name).exists():
        raise EmbedAppError("duplicate_name", f"an app named {name!r} is already registered")

    cleaned_origins = _clean_origins(origins)
    cleaned_keys = _clean_keys(public_keys or [])

    raw, app = AppCredential.create_credential(name=name, created_by=user)
    app.workspace_id = workspace_slug
    app.allowed_frame_origins = cleaned_origins
    app.public_keys = cleaned_keys
    app.jwks_url = _clean_jwks_url(jwks_url or "")
    app.save(update_fields=["workspace", "allowed_frame_origins", "public_keys", "jwks_url"])
    # Registering IS this tenant's own grant: the owner doing it has just said
    # the site may act for them. A second tenant grants itself separately.
    authorize_tenant(user=user, app=app, workspace_slug=workspace_slug,
                     resolvable_domains=resolvable_domains)
    set_agents(app, workspace_slug, agents or [])
    return raw, app


def update(*, user, app: AppCredential, workspace_slug: str, origins=None, agents=None,
           public_keys=None, resolvable_domains=None, jwks_url=None) -> AppCredential:
    """Change what a registered site may do, as ONE tenant.

    Two kinds of field, deliberately gated differently. The site's IDENTITY —
    its origins and its keys — belongs to the tenant that registered it, and
    another tenant editing it would be reaching into every other tenant's
    integration. What this tenant GRANTS — its own agents, its own resolvable
    domains — is this tenant's to change, and touches nobody else.
    """
    require_owner(user, workspace_slug)
    if tenant_grant(app, workspace_slug) is None:
        raise EmbedAppError(
            "not_granted", f"{app.name!r} is not authorized to act for this workspace")

    identity_fields = [f for f in (origins, public_keys, jwks_url) if f is not None]
    if identity_fields and app.workspace_id != workspace_slug:
        raise EmbedAppError(
            "not_owner",
            f"{app.name!r} is administered by another workspace — you can change what "
            "your own tenant grants it, but not the site's origins or keys",
        )

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
        authorize_tenant(user=user, app=app, workspace_slug=workspace_slug,
                         resolvable_domains=resolvable_domains)
    if agents is not None:
        set_agents(app, workspace_slug, agents)
    return app


def rotate(app: AppCredential) -> str:
    """Issue a new secret for an app, invalidating the old one immediately.

    The point of a rotate button is that the previous value stops working —
    someone reaches for it because the old one leaked. So this replaces the
    hash in place rather than creating a second credential, and any delegated
    token already minted keeps its own (short) life.
    """
    import hashlib
    import secrets

    raw = secrets.token_urlsafe(32)
    app.token_hash = hashlib.sha256(raw.encode()).hexdigest()
    app.save(update_fields=["token_hash"])
    return raw


def revoke(app: AppCredential) -> AppCredential:
    """Retire the SITE ITSELF, for everyone. Its embed shell 404s immediately
    and `issuer_of` stops resolving its name, so nothing it signs verifies.

    Almost never what a tenant means. One tenant leaving is `disconnect`, which
    calls this only when the last grant is gone — a site nobody grants is
    reachable by nobody, so retiring it takes nothing away. Left as the blunt
    instrument for staff (`admin.py`) and for that last-grant case.
    """
    if app.revoked_at is None:
        app.revoked_at = timezone.now()
        app.save(update_fields=["revoked_at"])
    return app


def disconnect(*, user, app: AppCredential, workspace_slug: str) -> AppCredential:
    """This tenant stops using the site. **Every other tenant is unaffected.**

    "Disconnect" on a tenant's page can only ever mean "we stop". It used to
    mean "retire this site for everyone": `revoke` set `revoked_at` on the site
    and `issuer_of` filters on it, so the workspace that happened to register a
    shared site could end every other tenant's integration with one button,
    and the button did not say so. That is the coupling this whole model exists
    to remove — a grant one tenant makes must not be endable by another.

    Two consequences follow, and both are about not stranding anyone:

    * **Custody transfers.** If the leaver was maintaining the site's origins
      and keys, the oldest remaining grant takes over. A site other tenants
      still use must never be left with nobody able to correct its key.
    * **The site is retired only when the last tenant leaves**, where retiring
      it takes nothing from anybody.
    """
    revoke_tenant(user=user, app=app, workspace_slug=workspace_slug)
    remaining = list(app.tenant_grants.order_by("created_at"))
    if not remaining:
        return revoke(app)
    if app.workspace_id == workspace_slug:
        app.workspace_id = remaining[0].workspace_id
        app.save(update_fields=["workspace"])
    return app

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
