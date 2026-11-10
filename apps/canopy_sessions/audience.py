""""Who can see this" — one line that says who a session is visible to.

Computed from the SAME pieces as the session ACL (`access.visible_session_q`):
each piece below is one leg, read for ONE session instead of for one person, and
the tenant gate is applied to every piece. `tests/test_session_audience.py`
holds the two to each other: for every session shape and every person, being
in `readers(session)` is exactly `access.can_read`. If a leg changes in
`access.py`, that test fails until the piece here changes with it.

The line is for people, so it names what they would recognise — the email
thread, the Slack channel, the machine, the agent — and says
"you" when the viewer is the one it is about. docs/architecture/access-sessions-and-people.md
§ Who can see a session.
"""
from __future__ import annotations

from django.utils import timezone

from apps.agents.models import AgentAdmin
from apps.harness import sensitivity
from apps.harness.models import Runner, RunnerAdmin
from apps.workspaces import services as wsvc

from .models import RunnerBinding, Session, SessionReviewAsk, SessionReviewGrant

#: Where an email-born session keeps its thread (`harness.session_sync.EMAIL_THREAD_KEY`).
EMAIL_THREAD_KEY = "email_thread_key"
#: Where a Slack-born session keeps its thread (`apps.slack.services.SLACK_THREAD_KEY`).
_SLACK_THREAD_KEY = "slack_thread"
#: The cached channel name (`apps.slack.location.LOCATION_KEY`).
_SLACK_LOCATION_KEY = "slack_location"


def _discovered(session) -> bool:
    return session.origin == Session.ORIGIN_RUNNER and session.created_by_id is None


def _binding(session):
    try:
        return session.runner_binding
    except RunnerBinding.DoesNotExist:
        return None


def _box_owners(session) -> set[int]:
    """Leg 3: the owner of the box a discovered session runs on (or, once it fell
    off the report, every owner of a box with the binding's host)."""
    if not _discovered(session):
        return set()
    b = _binding(session)
    if b is None:
        return set()
    if b.runner_id is not None:
        return {b.runner.owner_id} if b.runner.owner_id else set()
    if not b.host:
        return set()
    return set(Runner.objects.filter(host=b.host, owner__isnull=False).values_list("owner_id", flat=True))


def _agent_admins(session) -> set[int]:
    """Leg 4: the admins of the agent whose runner-origin session this is."""
    if session.origin != Session.ORIGIN_RUNNER or not session.agent_id:
        return set()
    agent = session.agent
    ids = set(AgentAdmin.objects.filter(agent=agent).values_list("user_id", flat=True))
    if agent.owner_id:
        ids.add(agent.owner_id)
    return ids | wsvc.owner_user_ids(agent.workspace_id)


def _own_login(session) -> set[int]:
    """Leg 4's other half: the agent's own login, on its own work."""
    if _discovered(session) and session.agent_id and session.agent.user_id:
        return {session.agent.user_id}
    return set()


def _coaches(session) -> dict[int, str]:
    """Leg 5: the logins of agents the session's person let review their sessions
    (`access.sessions_of_q`), never a sensitive or a contact's session. {user id: agent name}."""
    if session.contact_id or session.content_deleted_at or sensitivity.is_sensitive(session):
        return {}
    person = session.created_by_id
    if person is None and _discovered(session) and not session.agent_id:
        b = _binding(session)
        person = b.runner.owner_id if (b is not None and b.runner_id) else None
    if person is None:
        return {}
    grants = (SessionReviewGrant.objects.filter(user_id=person, revoked_at__isnull=True,
                                                agent__user__isnull=False).select_related("agent"))
    return {g.agent.user_id: (g.agent.name or g.agent.slug) for g in grants}


def _asked_reviewers(session) -> dict[int, str]:
    """Agents the session's person asked to review this one session: {agent id: name}. Not a
    leg (their turn reads as the person, `access.turn_reach`), so `readers` leaves them out;
    the line says it anyway, since it is who else will see the words."""
    if session.contact_id or sensitivity.is_sensitive(session):
        return {}
    person = session.created_by_id
    if person is None and _discovered(session) and not session.agent_id:
        b = _binding(session)
        person = b.runner.owner_id if (b is not None and b.runner_id) else None
    if person is None:
        return {}
    asks = (SessionReviewAsk.objects
            .filter(session=session, user_id=person, revoked_at__isnull=True, expires_at__gt=timezone.now(),
                    review_session__isnull=False)
            .select_related("agent"))
    return {a.agent_id: (a.agent.name or a.agent.slug) for a in asks}


def _runner_admins(session) -> set[int]:
    """Leg 6: admins of the box a discovered session is bound to."""
    b = _binding(session) if _discovered(session) else None
    if b is None or b.runner_id is None:
        return set()
    return set(RunnerAdmin.objects.filter(runner_id=b.runner_id).values_list("user_id", flat=True))


def pieces(session) -> dict:
    """Every leg's people for this session, each already inside the tenant."""
    members = wsvc.member_user_ids(session.workspace_id)
    within = lambda ids: set(ids) & members  # noqa: E731
    coaches = _coaches(session)
    asked = _asked_reviewers(session)
    return {
        "creator": within({session.created_by_id} if session.created_by_id else set()),
        "participants": within(session.participants.values_list("user_id", flat=True)),
        "box_owner": within(_box_owners(session)),
        "agent_admins": within(_agent_admins(session)),
        "own_login": within(_own_login(session)),
        "coaches": {uid: name for uid, name in coaches.items() if uid in members},
        "asked_reviewers": asked,
        "runner_admins": within(_runner_admins(session)),
    }


def readers(session) -> set[int]:
    """Everyone who may read `session` — the union of the pieces. A contact's own
    session (the panel) has no member readers beyond these."""
    p = pieces(session)
    out = p["creator"] | p["participants"] | p["box_owner"] | p["agent_admins"] | p["own_login"]
    out |= set(p["coaches"]) | p["runner_admins"]
    return out


def _name(user) -> str:
    return (user.get_full_name() or user.email or user.username) if user is not None else "someone"


def _added(n: int, you: bool) -> str:
    if n <= 0:
        return ""
    who = "you" if you else "they"
    return f" and {n} {'person' if n == 1 else 'people'} {who} added"


def summary(session, viewer) -> dict:
    """`{label, extras, count}` for `viewer`: one line naming who sees it, extra
    lines for who else may (a coach, a runner's admins), and how many people in all."""
    p = pieces(session)
    total = len(readers(session))
    vid = getattr(viewer, "pk", None)
    meta = session.metadata or {}
    others = set(p["participants"])
    extras: list[str] = []

    if session.contact_id:
        label = "The visitor who started it"
    elif meta.get(EMAIL_THREAD_KEY) or meta.get("source") == "email":
        label = f"Everyone on this email thread ({len(p['participants'])})"
    elif meta.get(_SLACK_THREAD_KEY):
        loc = meta.get(_SLACK_LOCATION_KEY) or {}
        channel = loc.get("channel_name") if isinstance(loc, dict) else None
        label = f"Members of #{channel}" if channel else "The people in this Slack conversation"
    elif _discovered(session) and not session.agent_id:
        owners = p["box_owner"]
        if vid in owners:
            label = "Only you" + _added(len(others - owners), True)
        else:
            owner = Runner.objects.filter(owner_id__in=owners).select_related("owner").first()
            label = f"{_name(owner.owner if owner else None)}'s machine" + _added(len(others - owners), False)
    elif session.origin == Session.ORIGIN_RUNNER and session.agent_id and session.created_by_id is None:
        agent = session.agent.name or session.agent.slug
        label = f"{agent}'s admins (the agent's own work)"
        if others:
            label += _added(len(others), False).replace(" they added", " added")
    elif session.parent_session_id:
        label = f"You and the people of the conversation it came from ({total})" if vid in readers(session) \
            else f"The people of the conversation it came from ({total})"
    else:
        creator = session.created_by
        added = len(others - {session.created_by_id})
        label = ("Only you" + _added(added, True)) if vid == session.created_by_id \
            else (_name(creator) + _added(added, False))

    for name in sorted(set(p["coaches"].values())):
        extras.append(f"{name} may review (coaching)")
    for name in sorted(set(p["asked_reviewers"].values())):
        extras.append(f"{name} may review this one session (asked from its page)")
    if p["runner_admins"] - p["box_owner"]:
        extras.append("This machine's admins can read it, to move it")
    return {"label": label, "extras": extras, "count": total}
