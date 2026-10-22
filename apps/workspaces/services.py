"""The workspace authorizer — the ONE place that decides who is in a workspace
and at what role (`member_role` / `is_member` / `membership`, the read scopes,
`creation_workspace`), plus the member mutations guarded against stranding a
workspace without an owner, and the only doors that create a membership
(`ensure_member`, `_grant`). `tests/test_workspace_authorizer_is_sole_gate.py`
pins the read side here.

How someone gets into a workspace (docs/architecture/access.md, "Getting into
a workspace"): they create it, an admin invites them and they accept
(`invites`), or they REQUEST an invitation (`access_requests`, open to people
whose login email is at one of the workspace's `access_request_domains`) and an
admin or owner approves it. Nothing joins anyone automatically: a domain match
makes a workspace REQUESTABLE, never joined (owner decision, 2026-10-04, which
replaced the self-join of 2026-09-12).

Elsewhere in this package: `links` (addresses under `/w/<ws>/`), `invites`,
`login_gate` (who may sign in or create a workspace), `access_requests`,
`notifications` (the shared email plumbing) and `vault` (the tenant's shared
1Password vault).
"""
from __future__ import annotations

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction

from apps.api.errors import ProblemError

from .models import (
    Workspace,
    WorkspaceMembership,
)

DEFAULT_WORKSPACE_SLUG = "dimagi"
DEFAULT_WORKSPACE_NAME = "Dimagi"


def allowed_domains() -> list[str]:
    raw = getattr(settings, "AUTH_ALLOWED_EMAIL_DOMAIN", "") or ""
    return [d.strip().lower() for d in raw.split(",") if d.strip()]


def _email_domain(email: str) -> str:
    email = (email or "").strip().lower()
    return email.rsplit("@", 1)[-1] if "@" in email else ""


def ensure_default_workspace() -> Workspace | None:
    """Return the default workspace, creating it (owned by the first superuser,
    else the first user) on first call. Returns None if there are no users yet
    (a fresh DB) — there is nothing to scope then."""
    ws = Workspace.objects.filter(slug=DEFAULT_WORKSPACE_SLUG).first()
    if ws is not None:
        return ws
    User = get_user_model()
    # Never the live probe's dedicated user (apps/host_grants/live_probe.py): on a
    # fresh deployment it can be the first row, and it must own nothing.
    probe = (getattr(settings, "CANOPY_HOST_PROBE_USERNAME", "") or "").strip()
    candidates = User.objects.exclude(username=probe) if probe else User.objects.all()
    owner = (
        candidates.filter(is_superuser=True).order_by("id").first()
        or candidates.order_by("id").first()
    )
    if owner is None:
        return None
    ws = Workspace.objects.create(
        slug=DEFAULT_WORKSPACE_SLUG,
        display_name=DEFAULT_WORKSPACE_NAME,
        created_by=owner,
        # Who may ASK to be invited — not who is in. Approval stays a person's
        # (auto-approve is off). The org default is the one workspace seeded
        # with a domain list; `create_workspace` never sets one.
        access_request_domains=allowed_domains(),
    )
    ensure_member(ws, owner, WorkspaceMembership.OWNER)
    return ws


def ensure_member(
    ws: Workspace, user, role: str = WorkspaceMembership.EDITOR, *, provisioned_by_app=None,
) -> tuple[WorkspaceMembership, bool]:
    """get_or_create — CREATE-ONLY: an existing member's role is never raised
    or lowered by this call, in either direction. `provisioned_by_app` (an
    `apps.tokens.models.AppCredential`) is recorded ONLY on create, so it
    marks provenance for an app-provisioned grant specifically —
    never retroactively attached to a pre-existing organic membership.
    Returns `(membership, created)` so a caller can
    tell whether it actually granted something worth logging."""
    m, created = WorkspaceMembership.objects.get_or_create(
        workspace=ws, user=user,
        defaults={"role": role, "provisioned_by_app": provisioned_by_app},
    )
    return m, created


def _direct_slugs(user, role: str | None = None) -> set[str]:
    qs = WorkspaceMembership.objects.filter(user=user)
    if role is not None:
        qs = qs.filter(role=role)
    return set(qs.values_list("workspace_id", flat=True))


def descendant_slugs(slugs: set[str]) -> set[str]:
    """Every workspace strictly BELOW any of `slugs` in the tree.

    Breadth-first, one query per level, bounded by `MAX_DEPTH` and a seen-set
    so a corrupted cycle terminates."""
    from .models import MAX_DEPTH

    out: set[str] = set()
    frontier = set(slugs)
    for _ in range(MAX_DEPTH):
        if not frontier:
            break
        children = set(
            Workspace.objects.filter(parent_id__in=frontier).values_list("slug", flat=True)
        ) - out - set(slugs)
        out |= children
        frontier = children
    return out


def subtree(root: Workspace, visible=lambda _slug: True) -> list[tuple[Workspace, int]]:
    """`root` and the descendants `visible` admits, depth-first in tree order,
    each with its depth below `root` (0 = root). A workspace whose parent is
    hidden hangs under its nearest visible ancestor, so a reader who holds a
    grandchild but not the child still sees it in place."""
    slugs = {root.slug} | {s for s in descendant_slugs({root.slug}) if visible(s)}
    by_parent: dict[str, list[Workspace]] = {}
    for ws in Workspace.objects.filter(slug__in=slugs - {root.slug}):
        parent = next((a for a in ws.ancestor_slugs() if a in slugs), root.slug)
        by_parent.setdefault(parent, []).append(ws)
    out: list[tuple[Workspace, int]] = []

    def walk(ws: Workspace, depth: int) -> None:
        out.append((ws, depth))
        for child in sorted(by_parent.get(ws.slug, []), key=lambda w: w.display_name.lower()):
            walk(child, depth + 1)

    walk(root, 0)
    return out


def _better(a: str | None, b: str | None) -> str | None:
    """The higher-ranked of two roles (either may be None)."""
    rank = WorkspaceMembership.ROLE_RANK
    if a is None:
        return b
    if b is None:
        return a
    return a if rank[a] >= rank[b] else b


def inherited_roles(user, direct: dict[str, str] | None = None) -> dict[str, str]:
    """{slug: role} for every workspace `user` holds a role in BY INHERITANCE:
    the descendants of a workspace where they are an owner or an admin
    (`WorkspaceMembership.INHERITS_DOWN`), each at the best such role held on
    any of its ancestors. What the tree grants — see `Workspace.parent`.

    `direct` is the caller's {slug: role} rows when already in hand (saves the
    query). Breadth-first, one query per level, bounded by `MAX_DEPTH` and a
    seen-set so a corrupted cycle terminates."""
    from .models import MAX_DEPTH

    if direct is None:
        direct = dict(WorkspaceMembership.objects.filter(user=user)
                      .values_list("workspace_id", "role"))
    # What each workspace passes to its children: its own inheriting role, or
    # what it inherited, whichever ranks higher.
    carry = {s: r for s, r in direct.items() if r in WorkspaceMembership.INHERITS_DOWN}
    out: dict[str, str] = {}
    frontier = dict(carry)
    seen = set(frontier)
    for _ in range(MAX_DEPTH):
        if not frontier:
            break
        nxt: dict[str, str] = {}
        for slug, parent in Workspace.objects.filter(parent_id__in=frontier).values_list("slug", "parent_id"):
            if slug in seen:
                continue
            got = frontier[parent]
            out[slug] = got
            own = direct.get(slug)
            nxt[slug] = _better(got, own if own in WorkspaceMembership.INHERITS_DOWN else None)
        seen |= set(nxt)
        frontier = nxt
    return out


def inherited_owner_slugs(user) -> set[str]:
    """Workspaces `user` OWNS by inheritance: descendants of a workspace they
    directly own."""
    return {s for s, r in inherited_roles(user).items() if r == WorkspaceMembership.OWNER}


def user_workspace_slugs(user) -> set[str]:
    """Every workspace `user` may act within: direct memberships plus the
    descendants of workspaces they own or administer."""
    direct = dict(WorkspaceMembership.objects.filter(user=user)
                  .values_list("workspace_id", "role"))
    return set(direct) | set(inherited_roles(user, direct))


def is_member(user, slug: str) -> bool:
    return membership(user, slug) is not None


def _inherited_role(user, workspace_id: str) -> str | None:
    """The best role `user` holds on any ANCESTOR of `workspace_id` that flows
    down (`INHERITS_DOWN`), or None."""
    ws = Workspace.objects.filter(slug=workspace_id).first()
    if ws is None or not ws.parent_id:
        return None
    roles = WorkspaceMembership.objects.filter(
        user=user, workspace_id__in=ws.ancestor_slugs(),
        role__in=WorkspaceMembership.INHERITS_DOWN,
    ).values_list("role", flat=True)
    best = None
    for role in roles:
        best = _better(best, role)
    return best


def membership(user, workspace):
    """The caller's membership row, lowered to their acting cap (`role_cap`) when one is
    set — see `_uncapped_membership` for the row itself."""
    from . import role_cap

    row = _uncapped_membership(user, workspace)
    cap = role_cap.cap_of(user)
    lowered = role_cap.capped_role(row.role if row is not None else None, cap)
    if row is None or lowered == row.role:
        return row
    # SYNTHETIC and unsaved, like an inherited row: a capped caller acts on no row.
    synthetic = WorkspaceMembership(workspace=row.workspace, user=user, role=lowered)
    synthetic.inherited = True
    return synthetic


def _uncapped_membership(user, workspace):
    """The caller's `WorkspaceMembership` ROW in `workspace`, or `None`.

    `member_role` answers the question; this returns the row, for the workspace
    admin surface that needs to act on the membership it just authorized (read
    its `workspace`, change its role, remove it). Kept here beside the other
    readers so that "am I in this workspace?" has exactly one implementation no
    matter which of the two shapes the caller needs —
    `tests/test_workspace_authorizer_is_sole_gate.py` enforces that.

    `select_related("workspace")` because every caller that wants the row wants
    the tenant with it.
    """
    workspace_id = workspace.pk if hasattr(workspace, "pk") else workspace
    row = (
        WorkspaceMembership.objects.select_related("workspace")
        .filter(user=user, workspace_id=workspace_id)
        .first()
    )
    if row is not None and row.role == WorkspaceMembership.OWNER:
        return row
    # An owner or admin of any ancestor is an owner or admin here (see
    # `Workspace.parent`). This outranks a weaker DIRECT row — an org admin who
    # also holds `viewer` on a division is still its admin. The returned row is
    # SYNTHETIC and unsaved (`inherited = True`): callers read `.role` /
    # `.workspace` off it, and anything that would persist or delete it must
    # check `inherited` first — there is no row to change; the grant lives on
    # the ancestor.
    up = _inherited_role(user, workspace_id)
    rank = WorkspaceMembership.ROLE_RANK
    if up is not None and (row is None or rank[up] > rank[row.role]):
        ws = row.workspace if row is not None else Workspace.objects.get(slug=workspace_id)
        synthetic = WorkspaceMembership(workspace=ws, user=user, role=up)
        synthetic.inherited = True
        return synthetic
    return row


def member_roles(user) -> dict[str, str]:
    """`member_role` for EVERY workspace the user is in, at once: {slug: role}.

    The same answer `membership` gives one workspace at a time — a direct row's
    role, except that owning or administering any ancestor makes you that here,
    outranking a weaker direct row — from one query for the direct rows and one
    per tree level for the inherited roles, instead of several queries per
    workspace. Built for the paths that ask it of every workspace on every poll
    (`runner_tenant_slugs`)."""
    if not getattr(user, "is_authenticated", False) and not getattr(user, "pk", None):
        return {}
    roles = dict(WorkspaceMembership.objects.filter(user=user)
                 .values_list("workspace_id", "role"))
    for slug, role in inherited_roles(user, roles).items():
        roles[slug] = _better(roles.get(slug), role)
    from . import role_cap

    cap = role_cap.cap_of(user)
    if cap:
        roles = {slug: role_cap.capped_role(role, cap) for slug, role in roles.items()}
    return roles


def member_role(user, workspace) -> str | None:
    """The caller's role in `workspace` (a `Workspace` or a bare slug), or
    `None` if they are not a member at all.

    The single place a ROLE — as opposed to bare membership — is read, so every
    gate that distinguishes viewer from editor agrees on where that comes from.
    `is_member(user, slug)` is exactly `member_role(...) is not None`; it stays
    because most call sites only need the boolean and reading it as one is
    clearer than comparing against None.

    Takes a bare `user` rather than a request so the request-free service
    layers (notably `apps/harness/schedule_services.py`, which the MCP tools
    call directly) can use the same reader the Ninja handlers do — the MCP
    invariant is that both surfaces run through one implementation, and an
    authorization check is the last thing that should have two."""
    m = membership(user, workspace)
    return m.role if m else None


def has_role_at_least(user, workspace, minimum: str) -> bool:
    """Does the caller hold `minimum` or better in `workspace`?

    Reads the ladder off `WorkspaceMembership.ROLE_RANK` rather than a
    hand-written set per call site, so adding a role between two existing ones
    does not silently widen a gate that happened to spell out its members."""
    role = member_role(user, workspace)
    if role is None:
        return False
    return WorkspaceMembership.ROLE_RANK.get(role, -1) >= WorkspaceMembership.ROLE_RANK[minimum]


def tree_roots(slugs) -> dict[str, str]:
    """`{slug: root}` — the top of each workspace's tree (its organisation).

    One query per tree level, bounded by `MAX_DEPTH` like `ancestor_slugs`, so
    a corrupted cycle degrades to a truncated walk rather than a hang. A slug
    with no row is its own root (fails toward "a separate tenant")."""
    from .models import MAX_DEPTH

    slugs = set(slugs)
    parent: dict[str, str | None] = {}
    frontier = set(slugs)
    for _ in range(MAX_DEPTH + 1):
        frontier -= set(parent)
        if not frontier:
            break
        rows = dict(Workspace.objects.filter(slug__in=frontier).values_list("slug", "parent_id"))
        for slug in frontier:
            parent[slug] = rows.get(slug)
        frontier = {p for p in rows.values() if p}
    out: dict[str, str] = {}
    for slug in slugs:
        cur, seen = slug, {slug}
        while parent.get(cur) and parent[cur] not in seen:
            cur = parent[cur]
            seen.add(cur)
        out[slug] = cur
    return out


def covering_pins(slugs) -> list[str]:
    """The fewest of `slugs` whose pinned reads (`pinned_read_scope`) together
    cover all of them: each one with no ANCESTOR among `slugs`. For ada@ in
    dimagi, connect and marshellis → [dimagi, marshellis] — one read per
    organisation when the caller is in its root. Sorted."""
    slugs = set(slugs)
    rows = dict(Workspace.objects.filter(slug__in=slugs).values_list("slug", "parent_id"))
    out = []
    for slug in slugs:
        ws = Workspace(slug=slug, parent_id=rows.get(slug))
        if not (set(ws.ancestor_slugs()) & slugs):
            out.append(slug)
    return sorted(out)


class WorkspaceRequired(ProblemError):
    """422: an UNPINNED read by a caller whose workspaces span more than one
    tenant tree. The union of their memberships would mix organisations
    (Dimagi's work with a family's), so the read must name one.

    A `ProblemError`, so a Ninja handler renders it as problem+json with no
    try/except; the MCP tools that call the services directly turn it into a
    `ToolError` (`apps/mcp/tools/sessions.py`)."""

    TYPE = "https://canopy-web.dimagi.com/problems/workspace-required"

    def __init__(self, slugs) -> None:
        roots = tree_roots(slugs)
        trees: dict[str, list[str]] = {}
        for slug, root in roots.items():
            trees.setdefault(root, []).append(slug)
        trees = {r: sorted(v) for r, v in sorted(trees.items())}
        listed = "; ".join(f"{r}: {', '.join(v)}" for r, v in trees.items())
        tops = covering_pins(roots)
        detail = (
            f"your workspaces span {len(trees)} organisations ({listed}), and a read "
            "that names no workspace would mix them. Name one: /api/w/<workspace>/… "
            "(the MCP tools take `workspace`; the canopy CLI reads CANOPY_WEB_WORKSPACE). "
            "A pinned read covers the workspaces below the pin that you belong to, so "
            f"one read per organisation is enough: {', '.join(tops)}"
        )
        # `pins`: the fewest pinned reads that together cover every one of the
        # caller's workspaces (`covering_pins`) — what a client loops over.
        super().__init__(422, "Workspace required", type_=self.TYPE, detail=detail,
                         extras={"workspaces": sorted(roots), "trees": trees, "pins": tops})
        self.detail = detail

    def __str__(self) -> str:
        return self.detail


def pinned_read_scope(user, pinned: str) -> set[str]:
    """What a read PINNED to `pinned` spans: that workspace plus every workspace
    BELOW it the caller is in (directly, or by owning an ancestor) — owner
    decision, Jonathan, 2026-10-12: "a pinned read of W returns W plus every
    descendant of W that the caller is a member of".

    `dimagi` covers `connect` for anyone in both, so one pinned read answers the
    whole organisation; `connect` does not cover `dimagi` (nothing above the pin);
    a root's subtree never reaches another root, so organisations still never
    mix. A descendant the caller is NOT in stays out — the pin widens to the
    caller's own memberships, never past them.

    Reads only. A write pinned to W lands in exactly W (`creation_workspace`), and
    a bulk write is scoped by `write_scope` — exactly the pin."""
    below = descendant_slugs({pinned})
    if not below or user is None or not getattr(user, "is_authenticated", False):
        return {pinned}
    return {pinned} | (below & user_workspace_slugs(user))


def read_scope(user, pinned: str | None = None) -> set[str]:
    """The workspaces a LIST or SEARCH may span for `user` — the read rule
    (owner decisions, Jonathan, 2026-10-11: "workspaces should need to be
    specified — workspace confusion shouldn't be possible"; 2026-10-12: a pinned
    read covers the workspaces below the pin).

    * pinned → that workspace and the caller's workspaces below it
      (`pinned_read_scope`; the caller already proved membership of the pin:
      `WorkspaceResolveMiddleware`, or the MCP `workspace` argument);
    * unpinned, and every membership sits in ONE tree (dimagi + connect) → the
      union, as before: within one organisation it is that org's own data;
    * unpinned, memberships in MORE THAN ONE tree (dimagi + marshellis) →
      `WorkspaceRequired` (422). Fails closed: nothing is guessed.

    By-id reads do NOT use this — see `member_scope`. Reading an object you are
    a member of by its id mixes nothing; a list across organisations does."""
    if pinned:
        return pinned_read_scope(user, pinned)
    if user is None or not getattr(user, "is_authenticated", False):
        return set()
    slugs = user_workspace_slugs(user)
    if len(slugs) > 1 and len(set(tree_roots(slugs).values())) > 1:
        raise WorkspaceRequired(slugs)
    return slugs


def write_scope(user, pinned: str | None = None) -> set[str]:
    """The workspaces a BULK WRITE may touch (clear every shareout, reset every
    session, flip a narrative's visibility across its rows): pinned → exactly
    that workspace, never the ones below it — "clear my shareouts" pinned to
    `dimagi` must not wipe `connect`'s. Unpinned → `read_scope` (one tree's
    union, or the 422 for a caller in several)."""
    if pinned:
        return {pinned}
    return read_scope(user)


def tree_scope(user, home: str) -> set[str]:
    """For a view of ONE object whose subject spans workspaces (a huddle's or a
    thread's members live in several): the caller's memberships in `home`'s
    tenant tree, `home` included. By-id, so it never refuses; it never crosses
    into another tree. Lists do not use it — a pinned list takes the read rule
    (`read_scope`), which already covers the workspaces below the pin."""
    if user is None or not getattr(user, "is_authenticated", False):
        return set()
    slugs = user_workspace_slugs(user) | {home}
    roots = tree_roots(slugs)
    return {s for s in slugs if roots[s] == roots[home]}


def member_scope(user, pinned: str | None = None) -> set[str]:
    """Every workspace `user` may act within, never refused — for a BY-ID gate
    (`obj.workspace_id in member_scope(...)`) and for system paths that are not
    a read of tenant data (routing, runners, the workspace switcher). Pinned →
    that workspace and the caller's workspaces below it (`pinned_read_scope`),
    the same span a pinned LIST has — so a row a pinned list shows is always one
    the by-id gate admits. Before 2026-10-17 it was exactly the pin: pinned to
    `dimagi`, `/api/w/dimagi/agents/hal` 404'd though `dimagi`'s agent list showed
    hal (in `connect`), and a `dimagi`-pinned write couldn't name a `connect` task.
    Never wider than unpinned (still only the caller's memberships); a bulk write
    stays `write_scope`, exactly the pin, and each object's role check is its own."""
    if pinned:
        return pinned_read_scope(user, pinned)
    if user is None or not getattr(user, "is_authenticated", False):
        return set()
    return user_workspace_slugs(user)


def _read_pin(request) -> str | None:
    """The workspace a request's LIST reads are pinned to: the `/api/w/{ws}/`
    pin, else — for a connected site acting for its visitor (a delegated token)
    — the site's own workspace, which is the only one it may act in
    (`apps.tokens.delegation`). A site is one tenant's; its visitor being in
    several must not make its chat feed refuse or mix them.

    The URL pin was membership-checked by `WorkspaceResolveMiddleware`; the
    site's workspace is checked by the callers below (`_site_outsider`)."""
    pinned = getattr(request, "workspace_slug", None)
    if pinned:
        return pinned
    return getattr(getattr(request, "delegated_app", None), "workspace_id", None) or None


def _site_outsider(request, pin: str | None) -> bool:
    """A site pinned the read to its workspace, and its visitor is not a member
    of it: the read reaches nothing (fails closed)."""
    if not pin or getattr(request, "workspace_slug", None):
        return False
    user = getattr(request, "user", None)
    return user is None or not getattr(user, "is_authenticated", False) or not is_member(user, pin)


def request_workspace_slugs(request) -> set[str]:
    """The workspace slugs a LIST or SEARCH on THIS request may span — the
    single place a flat (`/api/…`) handler gets its tenant scope, so scoping
    can't drift per endpoint. `read_scope` is the rule: pinned → that workspace
    and the caller's workspaces below it; unpinned → the caller's memberships
    when they sit in one tenant tree, else a 422 (`WorkspaceRequired`) naming
    them; empty for anonymous callers. A connected site acting for its visitor
    reads exactly the site's workspace (a site is one tenant's).

    Filter list querysets by `workspace_id__in=request_workspace_slugs(request)`.
    Gate a by-id read/mutation with `obj.workspace_id in request_member_slugs(request)`
    instead: that never refuses. Scope a BULK write by `request_write_slugs`.
    Either way this is the hard tenant boundary: data outside the caller's
    workspaces is unreachable."""
    pin = _read_pin(request)
    if _site_outsider(request, pin):
        return set()
    if pin and not getattr(request, "workspace_slug", None):
        return {pin}  # a site's own workspace: exactly it
    return read_scope(getattr(request, "user", None), pin)


def request_write_slugs(request) -> set[str]:
    """`write_scope` for a request: the workspaces a BULK write on it may touch —
    pinned → exactly the pin (never the workspaces below it); unpinned → the read
    rule. Narrow it by role with `perms.request_slugs_with`."""
    pin = _read_pin(request)
    if _site_outsider(request, pin):
        return set()
    return write_scope(getattr(request, "user", None), pin)


def each_organisation(request, read):
    """A USER-LEVEL view (`/api/me/…`): `read()` once per organisation the
    caller is in, the results concatenated — the caller's own list across every
    workspace, side by side. Each run is an ordinary pinned list read (the pin
    is a covering pin, `covering_pins`, which the caller belongs to), so every
    route's own read rule applies unchanged and no query ever joins two trees.
    Rows already carry their workspace; re-sort the result if order matters.

    Already pinned (`/api/w/<ws>/me/…`, or a connected site acting for its
    visitor) → one run, exactly as the list would take it."""
    if _read_pin(request):
        return list(read())
    user = getattr(request, "user", None)
    if user is None or not getattr(user, "is_authenticated", False):
        return []
    out = []
    try:
        for pin in covering_pins(user_workspace_slugs(user)):
            request.workspace_slug = pin
            out.extend(read())
    finally:
        request.workspace_slug = None
    return out


def request_member_slugs(request) -> set[str]:
    """The by-id twin of `request_workspace_slugs`: pinned → that workspace,
    else every membership, never refused (`member_scope`)."""
    return member_scope(getattr(request, "user", None), getattr(request, "workspace_slug", None))


def request_workspace_slugs_at_least(request, minimum: str) -> set[str]:
    """The subset of `request_write_slugs` where the caller holds `minimum`
    or better — the WRITE scope a bulk mutation filters by.

    `request_workspace_slugs` answers "what may I read?". A bulk write (clear
    every shareout, delete a narrative across its rows) must not be scoped by
    that answer, or a viewer's empty-filter clear wipes the tenant — nor by the
    pinned read's subtree, or a clear pinned to a parent wipes its children.
    This is the write scope narrowed by role, read through `has_role_at_least`
    so the ladder lives in one place. A pinned `/api/w/{ws}/` request is narrowed
    too: the middleware checked membership, not role. Refused like a list is,
    for a caller in more than one tree who names no workspace."""
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return set()
    return {
        slug for slug in request_write_slugs(request)
        if has_role_at_least(user, slug, minimum)
    }


def workspace_slugs_for_user_id(user_id, workspace: str | None = None, *,
                                write: bool = False) -> set[str]:
    """The workspace slugs a user (by pk) may LIST within — the MCP-side
    counterpart of `request_workspace_slugs`, for tools that carry a token
    subject rather than a request. `workspace` pins one (it must be a
    membership, else empty): a read then covers the user's workspaces below it
    too (`pinned_read_scope`); with `write=True` (a bulk write, e.g. purging
    session noise) exactly that workspace (`write_scope`). Same rule unpinned:
    in more than one tree → `WorkspaceRequired`. Empty set for an unresolvable
    user, so a scoped query fails CLOSED (sees/clears nothing) instead of
    falling back to global."""
    if user_id is None:
        return set()
    from django.contrib.auth import get_user_model

    user = get_user_model().objects.filter(pk=user_id).first()
    if user is None:
        return set()
    if workspace and not is_member(user, workspace):
        return set()
    return write_scope(user, workspace) if write else read_scope(user, workspace)


def effective_memberships(ws) -> list[WorkspaceMembership]:
    """Everyone who is IN `ws`, as `membership()` would answer for each of them.

    The listing counterpart of `membership`: a workspace's own rows are only half
    the answer, because an owner or admin of any ancestor is one here too. Every
    surface that listed `ws.memberships` directly — the members page, an agent's
    roster, who gets pushed, who hears a supervisor frame, the last-owner guard —
    silently left those inherited owners out, so the org's owners were invisible
    on, and unpushed by, every division they administer.

    Direct rows come back as they are (user prefetched). An ancestor owner or
    admin gets a SYNTHETIC unsaved row at that role — replacing a weaker direct
    row if they hold one,
    exactly as `membership()` answers for them. `inherited = True` marks it: there
    is nothing here to change or remove, the grant lives on the ancestor. Direct
    rows carry `inherited = False`.
    """
    ws = ws if hasattr(ws, "pk") else Workspace.objects.get(slug=ws)
    rows = {m.user_id: m for m in ws.memberships.select_related("user").order_by("joined_at")}
    for m in rows.values():
        m.inherited = False
    ancestors = ws.ancestor_slugs()
    if ancestors:
        upstream = (
            WorkspaceMembership.objects.filter(
                workspace_id__in=ancestors, role__in=WorkspaceMembership.INHERITS_DOWN,
            ).select_related("user").order_by("joined_at")
        )
        best: dict[int, WorkspaceMembership] = {}
        for up in upstream:
            held = best.get(up.user_id)
            if held is None or _better(held.role, up.role) != held.role:
                best[up.user_id] = up
        for user_id, up in best.items():
            mine = rows.get(user_id)
            if mine is not None and _better(mine.role, up.role) == mine.role:
                continue  # a direct row as strong already says it, and can be acted on
            # A copy, never the direct row mutated in place: a caller that saved
            # a row it was handed would otherwise persist an inherited grant.
            synthetic = WorkspaceMembership(
                workspace=ws, user=up.user, role=up.role, joined_at=up.joined_at,
            )
            synthetic.inherited = True
            rows[user_id] = synthetic
    return list(rows.values())


def member_user_ids(ws) -> set[int]:
    """User ids of everyone in `ws`, inherited owners and admins included."""
    return {m.user_id for m in effective_memberships(ws)}


def owner_user_ids(ws) -> set[int]:
    """User ids of everyone who OWNS `ws`: its direct owners plus the owners of
    every ancestor."""
    return {m.user_id for m in effective_memberships(ws) if m.role == WorkspaceMembership.OWNER}


def workspace_member_ids(ws) -> list[int]:
    """User ids of every member of a workspace, inherited owners included. Used
    to fan a supervisor update out to each member's live socket."""
    return sorted(member_user_ids(ws))


def user_default_workspace(user) -> Workspace | None:
    """The user's workspace when unambiguous — their sole membership, else None
    (0 or 2+ memberships). Used to resolve a default for headless PAT callers."""
    rows = list(
        WorkspaceMembership.objects.filter(user=user).select_related("workspace")[:2]
    )
    return rows[0].workspace if len(rows) == 1 else None


def creation_workspace(request) -> Workspace | None:
    """The workspace a CREATE lands in — resolved only from tenants the caller
    is already in. `None` means "cannot be resolved"; the caller turns that
    into a 422.

    THIS REPLACED THE IMPLICIT-ENROLMENT SHAPE, which five create endpoints had
    each hand-rolled a copy of (projects, shareouts, walkthroughs, reviews,
    issues):

        ws = pinned or ensure_default_workspace()
        ensure_member(ws, request.user)          # <- grants EDITOR

    On the flat mount `request.workspace_slug` is None, so `ws` was the org
    default (`dimagi`) *regardless of who was calling*, and `ensure_member`
    then granted them EDITOR of it as a side effect of posting a shareout.
    That was strictly broader than the self-join feature it coexisted with
    (since replaced by access requests), which at least required the caller's
    email domain to match, while this required nothing at all. An
    invite-admitted user — whose defining property is that they are NOT on the
    domain allowlist — became an editor of `dimagi` by creating one row, and
    from there passed every editor gate on the agent fleet.

    It also made `docs/architecture/roles.md` wrong where it says there are
    three ways into a workspace and "no automatic join". There are now three.

    Resolution, both legs membership-bound:

    1. Pinned `/api/w/{ws}/…` — `WorkspaceResolveMiddleware` already gated
       membership before setting `workspace_slug`, so this needs no recheck.
    2. Otherwise the caller's SOLE workspace (direct memberships plus what they
       own by inheritance) — unambiguous, so nothing is being guessed.
    3. Otherwise `None`: they belong to nothing, or to several workspaces and
       named none. `creation_refusal` says which, for the caller's 422.

    There used to be a leg between 1 and 2: "the org default, if the caller is
    a member". It silently filed a multi-workspace caller's writes in `dimagi`,
    which is how the chlorine demo narrative meant for `connect` (its reviewer
    was a connect member) landed where he could never find it — ACE is an
    editor in both, named neither, and every write succeeded (canopy-web#1289).
    Owner decision, 2026-10-08: nothing on canopy outside a workspace, and a
    caller in more than one must say which.

    Resolving a workspace is not permission to write in it: every caller still
    checks the editor role on what this returns.
    """
    pinned = getattr(request, "workspace_slug", None)
    if pinned:
        ws = Workspace.objects.filter(slug=pinned).first()
        if ws is not None:
            return ws
    user = getattr(request, "user", None)
    if user is None or not getattr(user, "is_authenticated", False):
        return None
    # Bootstraps a fresh deployment: the first user owns the org default, so
    # their first write has somewhere to land. A no-op once it exists.
    ensure_default_workspace()
    slugs = user_workspace_slugs(user)
    if len(slugs) != 1:
        return None
    return Workspace.objects.filter(slug=next(iter(slugs))).first()


def creation_refusal(request) -> str:
    """Why `creation_workspace` returned None — the `detail` of the 422.

    Names the caller's workspaces when there are several, because the fix is
    to pick one, and a refusal that does not say from what is a refusal people
    work around."""
    user = getattr(request, "user", None)
    slugs = (
        sorted(user_workspace_slugs(user))
        if user is not None and getattr(user, "is_authenticated", False)
        else []
    )
    if len(slugs) > 1:
        return (
            f"you belong to {len(slugs)} workspaces ({', '.join(slugs)}); name the one "
            "this belongs in: send it to /api/w/<workspace>/… (the MCP tools take "
            "`workspace`; the canopy CLI reads CANOPY_WEB_WORKSPACE)"
        )
    return "you do not belong to a workspace that can own this; ask an owner for an invite"


def current_workspace(user, explicit: str | None = None) -> Workspace:
    """Resolve the workspace a caller is acting in.

    explicit slug (caller must be a member) -> that workspace;
    else the caller's sole membership; else ValueError (none / ambiguous).
    Single resolution point for PAT callers, MCP tools, and the flat compat shim.
    """
    if explicit:
        ws = Workspace.objects.filter(slug=explicit).first()
        if ws is None or not is_member(user, explicit):
            raise ValueError(f"workspace '{explicit}' not found or not a member")
        return ws
    ws = user_default_workspace(user)
    if ws is None:
        raise ValueError("no unambiguous workspace for user; specify one")
    return ws


# ---- members ----


class MemberError(Exception):
    """Raised by `set_member_role` / `remove_member` for any reason a member
    mutation can't proceed.

    `.code` is a closed set (`not_found`, `last_owner`, `invalid_role`,
    `system_role` — a system account above editor) an HTTP layer maps to a status — same shape as `InviteError` below.
    """

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def is_last_owner(m: WorkspaceMembership) -> bool:
    """True iff `m` is an owner membership and the only owner left in its
    workspace. Shared by `remove_member` and `set_member_role` below so the
    "can't strand a workspace without an owner" guard lives in exactly one
    place. Must only be called from inside `_guarded_owner_mutation`'s
    transaction — see its docstring for why an un-locked call here is a
    TOCTOU hazard.

    An owner of an ANCESTOR counts: the workspace is not stranded while someone
    up the tree still owns it, so a direct owner may step down beside them.
    Counting only the direct rows refused exactly that — a division created by
    someone the org's owners already administer could never be handed back."""
    if m.role != WorkspaceMembership.OWNER:
        return False
    direct = WorkspaceMembership.objects.filter(
        workspace_id=m.workspace_id, role=WorkspaceMembership.OWNER
    ).count()
    if direct > 1:
        return False
    ancestors = m.workspace.ancestor_slugs()
    return not (
        ancestors
        and WorkspaceMembership.objects.filter(
            workspace_id__in=ancestors, role=WorkspaceMembership.OWNER,
        ).exists()
    )


def _guarded_owner_mutation(*, workspace: Workspace, user_id, mutate):
    """The single choke point for any mutation that can remove a workspace's
    last owner (a role change OR a removal) — both `set_member_role` and
    `remove_member` go through this so the "never zero owners" guard has
    exactly one transaction + lock boundary instead of two independent
    check-then-act windows.

    TOCTOU this closes: workspace with owners A and B. A demotes/removes B
    while B (in a truly concurrent request) demotes/removes A. Un-guarded,
    both read count(owner)==2, both pass `is_last_owner`, both commit — zero
    owners, workspace permanently unmanageable via the API. Locking every
    currently-OWNER row of the workspace (`select_for_update`) BEFORE
    fetching the target row means the second transaction blocks on the
    first's commit; Postgres then re-evaluates the WHERE clause against the
    post-commit row versions (EvalPlanQual), so by the time it proceeds the
    now-demoted-from-owner row no longer matches `role=OWNER` and the count
    correctly reads 1 — the second mutation is correctly rejected instead of
    both racing through. (sqlite — this repo's test DB — has no row locking;
    `has_select_for_update` is False so Django silently drops the FOR UPDATE
    clause. See test_member_roles.py's TOCTOU tests for what can and can't be
    proven against that backend.)

    `mutate(m)` does the actual role-change/delete once the target row (and,
    if it's currently an owner, the lock) is in hand; it may raise
    `MemberError('last_owner')` itself via `is_last_owner`.
    """
    with transaction.atomic():
        # Lock every currently-OWNER row of this workspace first, before even
        # fetching the target — this is what makes a concurrent mutation on a
        # DIFFERENT owner of the same workspace serialize against this one.
        list(
            WorkspaceMembership.objects.select_for_update().filter(
                workspace_id=workspace.pk, role=WorkspaceMembership.OWNER
            )
        )
        try:
            m = WorkspaceMembership.objects.select_related("user").get(
                workspace=workspace, user_id=user_id
            )
        except WorkspaceMembership.DoesNotExist:
            raise MemberError("not_found")
        return mutate(m)


def set_member_role(*, workspace: Workspace, user_id, role: str, by=None) -> WorkspaceMembership:
    """Change a member's role. Idempotent: setting the role a member already
    holds succeeds as a no-op (and, deliberately, never trips the last-owner
    guard — see `test_set_member_role_is_idempotent`). Raises
    `MemberError('invalid_role')` if `role` isn't one `ROLE_RANK` knows (a
    caller that bypassed the HTTP schema's `Literal`, e.g. a shell/MCP call —
    `.save()` doesn't enforce Django `choices`, and an unranked role would
    later KeyError inside `accept_invite`), `MemberError('not_found')` if
    `user_id` isn't a member of `workspace`, or `MemberError('last_owner')`
    if this would demote the workspace's only remaining owner. See
    `_guarded_owner_mutation` for the transaction/lock this runs inside.

    A demotion within this workspace needs no sweep of its own: every grant
    that hangs off being here re-reads the role when it is used (an agent
    admin is checked against membership, editor gates against the role), and
    the person is still in the tenant. What a demotion CAN do is take an owner
    or admin out of the workspaces below this one, which they only reached by
    inheritance — so those are swept exactly as a removal would sweep them
    (`departure.sweep`, which touches only workspaces they are no longer in).
    """
    if role not in WorkspaceMembership.ROLE_RANK:
        raise MemberError("invalid_role")

    def _mutate(m: WorkspaceMembership) -> WorkspaceMembership:
        if role != m.role:
            from .system_accounts import ROLES as SYSTEM_ROLES
            from .system_accounts import is_system_user

            if role not in SYSTEM_ROLES and is_system_user(m.user):
                # A system administers nothing (system_accounts.py).
                raise MemberError("system_role")
            if is_last_owner(m):
                raise MemberError("last_owner")
            carried_down = m.role in WorkspaceMembership.INHERITS_DOWN
            m.role = role
            m.save(update_fields=["role"])
            if carried_down:
                from . import departure

                departure.sweep(m.user, workspace.slug, by=by)
        return m

    return _guarded_owner_mutation(workspace=workspace, user_id=user_id, mutate=_mutate)


def remove_member(*, workspace: Workspace, user_id, by=None) -> None:
    """Remove a member. Raises `MemberError('not_found')` if `user_id` isn't
    a member of `workspace`, or `MemberError('last_owner')` if this would
    remove the workspace's only remaining owner. See `_guarded_owner_mutation`
    for the transaction/lock this runs inside — moved here (out of the API
    view) so it shares that boundary with `set_member_role` instead of each
    route running its own independent, unlocked check-then-act.

    Also revokes everything the membership carried (`departure.sweep`): agent
    and runner admin grants, chat participation, lent GitHub tokens, agent
    ownership, their box on this tenant's agents — and closes their live chat
    sockets here. In the same transaction, so a removal never half-happens.
    """

    def _mutate(m: WorkspaceMembership) -> None:
        if is_last_owner(m):
            raise MemberError("last_owner")
        m.delete()
        from . import departure

        departure.sweep(m.user, workspace.slug, by=by)

    _guarded_owner_mutation(workspace=workspace, user_id=user_id, mutate=_mutate)


# ---- the membership-creating door shared by invites and access requests ----


def _grant(ws: Workspace, user, role: str, invited_by=None) -> WorkspaceMembership:
    """Create the membership, or raise an existing one to `role`. Upgrade-only,
    exactly like `accept_invite`: a grant never demotes anyone."""
    m, created = WorkspaceMembership.objects.get_or_create(
        workspace=ws, user=user, defaults={"role": role, "invited_by": invited_by},
    )
    if not created and WorkspaceMembership.ROLE_RANK[role] > WorkspaceMembership.ROLE_RANK[m.role]:
        m.role = role
        m.save(update_fields=["role"])
    return m
