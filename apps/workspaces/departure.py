"""What leaving a workspace takes with it.

Removing a membership row used to be the whole of removal. Everything the
person had been granted *because* they were a member stayed behind — agent
admin rows, runner admin rows, chat participant rows, the GitHub token they
lent an agent, the agents they owned, and their own box's place on those
agents' runner lists. Most of those legs re-check membership at read time, so
the leftovers were dormant rather than live. But dormant is the problem:
`dimagi` auto-approves access requests (as editor, for now), so a removed
person asks again, lands as an editor, and every one of those grants wakes up
again. Removal you can undo yourself
is not removal.

So a departure is swept here, from one registry, rather than by a receiver per
app: each app that hangs a grant off membership registers a revoker from its
AppConfig.ready() (`apps/<app>/departure.py`), and workspaces names none of
them. `tests/test_departure_revokers.py` lists the expected names, so a missing
registration fails. Register one when you add a grant that hangs off being in a
workspace.

Scope is every workspace the person is NO LONGER in after the change — the one
they left, and any below it they only reached as an inherited owner or admin
(removing, or demoting, an org owner or admin takes the divisions with it). A workspace they are
still in through another path keeps their grants.
"""
from __future__ import annotations


def workspaces_left(user, slug: str) -> list[str]:
    """`slug` and its descendants that `user` no longer belongs to."""
    from . import services

    candidates = [slug, *sorted(services.descendant_slugs({slug}))]
    still_in = services.member_roles(user)
    return [s for s in candidates if s not in still_in]


# Each app that hangs a grant off being in a workspace registers one revoker
# here from its AppConfig.ready(); workspaces itself names none of them.
_REVOKERS: dict[str, tuple[int, object, bool]] = {}


def register(name: str, fn, *, order: int, counted: bool = True) -> None:
    """Register a revoker: `fn(user, left, by)` revokes what `user` held in the
    workspace slugs `left`. A counted revoker returns how many rows it removed
    (its `name` is the key in `sweep`'s result); an uncounted one returns None
    (a commit hook, say). Revokers run in ascending `order`, in the caller's
    transaction. Registering a name twice replaces it (ready() may run again)."""
    _REVOKERS[name] = (order, fn, counted)


def revoker_names() -> list[str]:
    """The registered revokers' names, in the order `sweep` runs them."""
    return [n for n, _ in _ordered()]


def _ordered() -> list[tuple[str, tuple[int, object, bool]]]:
    return sorted(_REVOKERS.items(), key=lambda kv: kv[1][0])


def sweep(user, slug: str, *, by=None) -> dict:
    """Revoke what `user` held in every workspace they have just left.

    Returns counts per kind, for tests and the Event. Runs inside the caller's
    transaction; the socket close is published on commit, and is best-effort.
    """
    left = workspaces_left(user, slug)
    counts = {name: 0 for name, (_, _, counted) in _ordered() if counted}
    if not left:
        return counts
    for name, (_, fn, counted) in _ordered():
        result = fn(user, left, by)
        if counted:
            counts[name] = result
    return counts
