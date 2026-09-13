"""Who may see a session — ONE predicate, used by both the list and by-id reads.

It lives in its own module because the two used to be written by hand at two
sites and disagreed, in both directions:

* **by-id was too loose.** `_session_or_404` gated on workspace membership
  alone, so any co-tenant holding a session UUID could read a conversation the
  list already refused to show them.
* **the list was wrong about *why*.** It granted co-tenant visibility to
  anything with a `RunnerBinding` — but a *web* session acquires a binding the
  moment a runner picks it up, so a private chat silently became co-tenant
  readable as soon as it started running. `origin` is the property that
  actually distinguishes "nobody in-app created this" from "someone did".

This is the same shape of guard `apps/harness/services.py` uses for
claim-vs-schedule after those two hand-written predicates drifted
(`tests/test_claim_schedule_parity.py`): share the predicate, then assert the
two callers agree, so a re-divergence fails CI instead of production.

Runner-DISCOVERED sessions stay visible to the whole tenant deliberately. They
are created with no `created_by` and no participant row (`harness/services.py`
`Session.objects.create(..., origin=ORIGIN_RUNNER)`), so gating them on
ownership or participation would make them unreachable by *everyone* — that is
the entire emdash-discovered-session flow, not an edge case.
"""

from __future__ import annotations

from django.db.models import Q

from .models import Session


def visible_session_q(user) -> Q:
    """Sessions `user` may read, *within a workspace they already belong to*.

    Tenancy is a separate and prior gate (`_visible_slugs`) — this narrows
    inside it and must never be used as the only check.

    Three ways in, and no fourth:

    1. you created it;
    2. you were made a participant — `SessionParticipant` is what "multiplayer"
       means here, and its docstring already calls itself "the authority for
       access and role";
    3. it is a runner-discovered session that a runner is actually reporting,
       which has no creator to belong to.

    Leg 3 keeps `runner_binding__isnull=False` alongside the origin check
    rather than dropping it: origin alone would newly expose runner-origin rows
    that no runner has ever bound (e.g. an email-thread session), which is a
    widening nobody asked for. Both conditions together reproduce exactly
    today's runner-session visibility.

    **Known consequence — an orphaned WEB session becomes invisible.**
    `Session.created_by` is `on_delete=SET_NULL`, so deleting a user leaves
    their web sessions with no creator and no participant, and leg 3 does not
    catch them because their origin is `web`. They stay in the database and
    remain reachable through the admin, but they drop out of the API for
    everyone.

    That is deliberate: the alternative rule — leg 3 as
    `Q(created_by__isnull=True) & Q(runner_binding__isnull=False)`, i.e. "nobody
    owns it, so the tenant may see it" — would hand a departed colleague's
    private conversations to every co-tenant the moment they were offboarded.
    Losing them from a list is the cheaper mistake than publishing them. Switch
    that one leg if the trade should go the other way; nothing else depends on
    the choice.
    """
    return (
        Q(created_by=user)
        | Q(participants__user=user)
        | (Q(origin=Session.ORIGIN_RUNNER) & Q(runner_binding__isnull=False))
    )
