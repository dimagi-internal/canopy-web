"""Insights changed — tell any page that is showing them.

**Why this lives in `projects` and not beside the machinery it calls.**
`canopy_sessions` is FRAMEWORK tier and `projects` is PRODUCT; framework must
never import product (ARCHITECTURE.md, enforced by
`tests/test_architecture_boundary.py`). So the generic half — dirty-set
coalescing, finding attached pages, publishing — lives in
`canopy_sessions.invalidation` and knows nothing about insights, while the one
line that says "an insight is `insight://`" lives here, in the app that owns the
row. Adding a second invalidated resource is a file like this one, in whichever
app owns it.

**Why a signal and not a call in each mutating service.** There are several ways
an insight dies — the REST clear endpoint, `clear_insights` over MCP, the page
action, a fleet turn, the admin — and "remember to notify" at each of them is N
sites that rot. This repo's own evidence for that is not theoretical:
`page_tools.py` shipped with ten passing tests that nothing imported, and six
tenancy predicates each independently grew a `NULL means allow` leg. One receiver
on the real row cannot be forgotten by a future author, which is the entire
argument. `apps/push/signals.py` made the same call for the same reason.
"""

from __future__ import annotations

from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from apps.canopy_sessions.invalidation import mark_dirty

from .models import ProjectContext

#: The resource URI an insight belongs to.
#:
#: MCP's vocabulary, not one of ours, so that when FastMCP grows a server-side
#: subscription API this string is already the thing an agent would subscribe
#: to. Collection-level: a page showing a filtered list still wants to know that
#: the set changed, and per-row URIs would make it subscribe to rows it has not
#: got yet.
INSIGHT_RESOURCE = "insight://"

#: The `ProjectContext.context_type` the insights feed shows.
#:
#: The same literal `insights_queryset` filters on
#: (`ProjectContext.objects.filter(context_type="insight")`). Named here so the
#: two are greppable together: a drift where rows are invalidated but never
#: listed — or listed and never invalidated — would be invisible at runtime, and
#: `test_page_invalidation` asserts they agree.
INSIGHT_CONTEXT_TYPE = "insight"


@receiver([post_save, post_delete], sender=ProjectContext)
def _insight_changed(sender, instance: ProjectContext, **kwargs) -> None:
    """Mark the insight collection dirty when an insight row moves.

    `ProjectContext` carries five kinds of row (`current_work`, `next_step`,
    `summary`, `note`, `insight`), so this filters rather than firing for all of
    them — a page showing insights must not refetch because a summary was
    written. `mark_dirty` coalesces, so a bulk clear of two hundred rows in one
    transaction still sends one notification per resource.
    """
    if instance.context_type != INSIGHT_CONTEXT_TYPE:
        return
    mark_dirty(INSIGHT_RESOURCE)
