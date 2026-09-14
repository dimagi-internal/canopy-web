"""The /api/contacts surface — who a workspace knows.

Read at the interaction tier and written at the author tier, matching the role
ladder in `docs/architecture/roles.md`: a viewer answering an agent's question
about a correspondent needs to see who they are, while correcting the tenant's
record of a person is a change to the tenant's knowledge.

There is deliberately no create route. Contacts arrive by being written to —
`services.record_inbound_sender`, called from the email turn path — and a
hand-created one would be a person canopy claims to know on no evidence.
"""
from __future__ import annotations

from django.http import HttpRequest
from ninja import Router
from ninja.errors import HttpError

from apps.api.auth import session_auth
from apps.api.errors import TYPE_NOT_FOUND, ProblemError
from apps.api.pagination import Page, clamp_limit, clamp_offset, paginate
from apps.workspaces import services as wsvc

from . import services
from .models import Contact
from .schemas import ContactOut, ContactPatchIn

router = Router(auth=session_auth, tags=["contacts"])


def _serialize(c: Contact) -> dict:
    return {
        "id": c.pk,
        "identity": c.identity,
        "source": c.source,
        "email": c.email,
        "external_id": c.external_id,
        "app_name": c.app.name if c.app_id else "",
        "display_name": c.display_name,
        "workspace_id": c.workspace_id,
        "is_user": c.user_id is not None,
        "auth_result": c.auth_result,
        "last_auth_result": c.last_auth_result,
        "notes": c.notes,
        "attributes": c.attributes or {},
        "message_count": c.message_count,
        "is_blocked": c.is_blocked,
        "blocked_reason": c.blocked_reason,
        "first_seen_at": c.first_seen_at,
        "last_seen_at": c.last_seen_at,
    }


def _visible_workspace_ids(request: HttpRequest) -> set[str]:
    """The tenants whose contacts this caller may see.

    `wsvc.request_workspace_slugs` is the single place this repo answers that
    question — pinned to one workspace on `/api/w/{ws}/…`, all memberships on
    the flat mount. Built from it rather than hand-rolled, because a
    hand-rolled tenant predicate is how this codebase grew six independent
    copies that disagreed.
    """
    return wsvc.request_workspace_slugs(request)


def _contact_or_404(request: HttpRequest, contact_id: int) -> Contact:
    """Resolve, gated by tenant. A non-member gets 404, never 403 — a contact
    is a person's identity and this API must not confirm that a given address
    is known to a tenant the caller cannot see."""
    c = Contact.objects.select_related("user").filter(pk=contact_id).first()
    if c is None or c.workspace_id not in _visible_workspace_ids(request):
        raise ProblemError(404, "Contact not found", type_=TYPE_NOT_FOUND)
    return c


@router.get("/", response=Page[ContactOut], summary="People this workspace knows")
def list_contacts(
    request: HttpRequest, q: str | None = None, offset: int = 0, limit: int = 50,
) -> Page[ContactOut]:
    """Scoped to the caller's tenants. `q` filters on address or display name."""
    offset, limit = clamp_offset(offset), clamp_limit(limit)
    qs = Contact.objects.filter(
        workspace_id__in=_visible_workspace_ids(request)
    ).select_related("user")
    if q:
        from django.db.models import Q

        qs = qs.filter(Q(email__icontains=q) | Q(display_name__icontains=q))
    return paginate([ContactOut.model_validate(_serialize(c)) for c in qs],
                    offset=offset, limit=limit)


@router.get("/{contact_id}/", response=ContactOut, summary="One person")
def get_contact(request: HttpRequest, contact_id: int) -> ContactOut:
    return ContactOut.model_validate(_serialize(_contact_or_404(request, contact_id)))


@router.patch("/{contact_id}/", response=ContactOut,
              summary="Correct what we know about a person (editor)")
def patch_contact(request: HttpRequest, contact_id: int, payload: ContactPatchIn) -> ContactOut:
    """Editor or better: this is the tenant's record of a person, and changing
    it changes what an agent will believe on the next turn.

    Resolve-then-authorize, the ordering used everywhere else in this codebase:
    a non-member gets 404 from `_contact_or_404` above and never reaches the
    403, so this never confirms a contact exists to someone who cannot see it.
    """
    contact = _contact_or_404(request, contact_id)
    if not wsvc.has_role_at_least(
        request.user, contact.workspace_id, wsvc.WorkspaceMembership.EDITOR
    ):
        raise HttpError(403, "editing a contact requires the editor or owner role")

    data = payload.model_dump(exclude_unset=True, exclude_none=True)

    # `blocked` is a verb, not a column: it sets a timestamp and a reason
    # together, through the service, so the two cannot drift apart. Popped
    # before the generic loop, which would otherwise set a bogus attribute and
    # then hand `update_fields` a name the model does not have.
    blocked = data.pop("blocked", None)
    reason = data.pop("blocked_reason", None)

    for field, value in data.items():
        setattr(contact, field, value)
    if data:
        contact.save(update_fields=[*data.keys(), "last_seen_at"])

    if blocked is True:
        services.block(contact, reason=reason or "")
    elif blocked is False:
        services.unblock(contact)
    elif reason is not None and contact.is_blocked:
        # A reason on its own only makes sense for someone already refused.
        services.block(contact, reason=reason)

    return ContactOut.model_validate(_serialize(contact))
