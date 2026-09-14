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

**What the surface deliberately will not do.** It does not grant provisioning
(`provision_workspace`), which lets a credential add users to a tenant. That is
a different and larger power than embedding, it has never been needed by a
widget, and a form is the wrong place to hand it out. Nothing here can set it,
so an app registered this way can only ever be embedded.
"""

from __future__ import annotations

from django.db.models import QuerySet
from django.utils import timezone

from apps.workspaces.models import WorkspaceMembership
from apps.workspaces.services import allowed_domains

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
    if not WorkspaceMembership.objects.filter(user=user, workspace_id=slug).exists():
        raise EmbedAppError("not_found", f"workspace {slug!r} not found")
    if slug not in owned_workspace_slugs(user):
        raise EmbedAppError("not_owner", "only a workspace owner can manage connected apps")


def apps_for(user, slug: str) -> QuerySet[AppCredential]:
    """The apps this user may administer in `slug`.

    Filtered by `workspace_id__in`, never `== slug`, and that is the whole
    guard: a SQL `IN` cannot match NULL, so a credential predating this surface
    (which has no owning workspace) is excluded by construction rather than by
    remembering to exclude it. A nullable tenant FK read as "no tenant ⇒
    allowed" is the exact bug ARCHITECTURE.md records against `Agent.workspace`.
    """
    require_owner(user, slug)
    return (
        AppCredential.objects.filter(workspace_id__in={slug} & owned_workspace_slugs(user))
        .prefetch_related("allowed_agents__agent")
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


def _clean_domains(domains, *, actor) -> list[str]:
    """Validate the delegation domains against what the ACTOR may vouch for.

    A domain here is the strongest thing on this page: whoever holds the app's
    secret can exchange it for a token acting as ANY canopy user with an email
    in that domain. Two bounds, both required.

    The domain must be one canopy already admits at login, so this cannot widen
    who may reach canopy — that is a policy change, not a form field. And it
    must be the actor's OWN domain, so an owner can only extend the reach they
    already have rather than mint access to a population they are not part of.
    """
    if not isinstance(domains, list):
        raise EmbedAppError("bad_domain", "expected a list of email domains")
    mine = (getattr(actor, "email", "") or "").strip().lower().rsplit("@", 1)[-1]
    admitted = set(allowed_domains())
    cleaned: list[str] = []
    for raw in domains:
        value = (raw or "").strip().lower().lstrip("@") if isinstance(raw, str) else raw
        if not value:
            continue
        if "@" in value or "/" in value:
            raise EmbedAppError(
                "bad_domain",
                f"{raw!r} is not a domain. Use the bare domain (dimagi.com), not an "
                "address and not a URL.",
            )
        if value != mine:
            raise EmbedAppError(
                "bad_domain",
                f"you can only vouch for your own email domain ({mine or 'unknown'}), "
                f"not {value!r}. This grant lets the app act as any canopy user in "
                "the domain, so it is bounded by the reach you already have.",
            )
        if value not in admitted:
            raise EmbedAppError(
                "bad_domain",
                f"{value!r} is not a domain canopy accepts at login, so a token for "
                "it could never be used. Widening that is a deployment policy "
                "change (AUTH_ALLOWED_EMAIL_DOMAIN), not a setting on this page.",
            )
        if value not in cleaned:
            cleaned.append(value)
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


def set_agents(app: AppCredential, slugs: list[str]) -> None:
    """Replace the agents this app may offer.

    Only agents in the app's OWN workspace. The picker already intersects the
    allowlist with the viewer's memberships, so a foreign agent here would be
    invisible to everyone and read as a grant that silently does nothing —
    worse than a refusal. Offering another tenant's agent is that tenant's call
    to make, and there is deliberately no way to make it from this page.
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
            f"not agents in this workspace: {', '.join(missing)}. An app can only "
            "offer agents belonging to the workspace that owns it.",
        )
    AppCredentialAgent.objects.filter(app=app).exclude(agent__slug__in=wanted).delete()
    for slug in wanted:
        AppCredentialAgent.objects.get_or_create(app=app, agent=found[slug])


def register(*, user, workspace_slug: str, name: str, origins: list[str],
             domains: list[str] | None = None, agents: list[str] | None = None,
             public_keys: list[str] | None = None,
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
    cleaned_domains = _clean_domains(domains or [], actor=user)
    cleaned_keys = _clean_keys(public_keys or [])

    raw, app = AppCredential.create_credential(
        name=name, domains=cleaned_domains, created_by=user,
    )
    app.workspace_id = workspace_slug
    app.allowed_frame_origins = cleaned_origins
    app.public_keys = cleaned_keys
    app.save(update_fields=["workspace", "allowed_frame_origins", "public_keys"])
    set_agents(app, agents or [])
    return raw, app


def update(*, user, app: AppCredential, origins=None, domains=None, agents=None,
           public_keys=None) -> AppCredential:
    """Change what an already-registered app may do. Every field is optional."""
    fields: list[str] = []
    if origins is not None:
        app.allowed_frame_origins = _clean_origins(origins)
        fields.append("allowed_frame_origins")
    if domains is not None:
        app.allowed_delegation_domains = _clean_domains(domains, actor=user)
        fields.append("allowed_delegation_domains")
    if public_keys is not None:
        app.public_keys = _clean_keys(public_keys)
        fields.append("public_keys")
    if fields:
        app.save(update_fields=fields)
    if agents is not None:
        set_agents(app, agents)
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
    """Disconnect a site. Its embed shell 404s immediately."""
    if app.revoked_at is None:
        app.revoked_at = timezone.now()
        app.save(update_fields=["revoked_at"])
    return app


# --- canopy's own widget, as one button ---------------------------------------


def self_app_name() -> str:
    """The credential name this deployment looks for on its own pages.

    Read from `EMBED_SELF_APP` rather than hard-coded, because the lookup in
    `embed_api._self_app()` reads the same setting — a literal here would be a
    second source of truth for a fact whose only failure mode is silence (a
    mismatched name resolves to no credential and the widget simply does not
    mount).
    """
    from django.conf import settings

    return (getattr(settings, "EMBED_SELF_APP", "") or "").strip()


def self_app() -> AppCredential | None:
    name = self_app_name()
    return AppCredential.objects.filter(name=name).first() if name else None


def enable_self(*, user, workspace_slug: str, origin: str, agents: list[str] | None = None):
    """Turn canopy's own widget on for this deployment, in one act.

    Everything a person would otherwise have to know — the exact name, that the
    delegation list must be empty, that a frame origin is required even though
    the frame is same-origin — is a fact about canopy, not a decision the user
    should be asked to make. Asking produced the two ways this goes wrong in
    practice: a name that does not match `EMBED_SELF_APP`, so nothing mounts and
    nothing says why; and a delegation domain granted by reflex, which turns a
    credential that can only frame a shell into one that can act as any user in
    that domain.

    Idempotent: run again to add an origin (a second environment) or change the
    agents, rather than failing on the name already existing.
    """
    name = self_app_name()
    if not name:
        raise EmbedAppError(
            "not_configured",
            "this deployment does not have EMBED_SELF_APP set, so canopy does not "
            "offer the widget on its own pages.",
        )
    require_owner(user, workspace_slug)

    existing = self_app()
    if existing is None:
        # No delegation domains, ever: `POST /api/embed/token` is
        # session-authenticated and mints for the caller, so the self-embed has
        # no assertion to make and nothing to vouch for.
        raw, app = AppCredential.create_credential(name=name, domains=[], created_by=user)
        app.workspace_id = workspace_slug
        app.allowed_frame_origins = _clean_origins([origin])
        app.save(update_fields=["workspace", "allowed_frame_origins"])
        set_agents(app, agents or [])
        # The secret is returned for completeness and is not needed by anyone:
        # nothing exchanges it. Callers may discard it.
        return raw, app

    app = existing
    fields = []
    wanted = _clean_origins([origin])
    if wanted and wanted[0] not in (app.allowed_frame_origins or []):
        app.allowed_frame_origins = [*(app.allowed_frame_origins or []), wanted[0]]
        fields.append("allowed_frame_origins")
    if app.revoked_at is not None:
        app.revoked_at = None
        fields.append("revoked_at")
    if app.workspace_id is None:
        # Adopt a row left over from before this page existed, so it becomes
        # editable rather than remaining a thing nobody can reach.
        app.workspace_id = workspace_slug
        fields.append("workspace")
    if fields:
        app.save(update_fields=fields)
    if agents is not None:
        set_agents(app, agents)
    return None, app
