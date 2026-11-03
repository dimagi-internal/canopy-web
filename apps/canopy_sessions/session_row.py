"""One session as a row of the session list (`SessionOut`): the shape every
list, detail and panel reads, built in one place."""
from __future__ import annotations

import uuid

from apps.harness import clis, provenance, sensitivity
from apps.harness import runner_requirements as rr
from apps.harness.models import Runner

from . import (
    access,
    channel_bindings,
    control,
    feed,
    serializers,
    session_state,
    staleness,
    transcript_read,
)
from . import activity as session_activity
from .models import Session


def _runner_online(runner) -> bool | None:
    """Liveness of a session's bound runner, or None when there is no binding."""
    if runner is None:
        return None
    return runner.live_status == Runner.ONLINE


def _runner_signed_out(runner, session) -> bool:
    """The bound box cannot run this conversation's agent CLI: its login for it is
    gone (`harness.clis.reported`). A conversation with no CLI yet reads Claude Code's."""
    if runner is None:
        return False
    report = clis.reported(runner).get(session.cli or clis.CLAUDE_CODE)
    return report is not None and not report.signed_in


def _runner_status(runner) -> str | None:
    """The bound runner's live_status verbatim — see SessionOut.runner_status.

    Read from the same property `_runner_online` derives its bool from, so the
    two can never disagree about a runner (one says offline, the other online).
    """
    return None if runner is None else runner.live_status


def _requested_runner_name(session: Session) -> str | None:
    """The name of the runner a not-yet-bound chat was started on (the stash
    `create_session` leaves for its first send), so a list can say where it will
    run before it has. Only unbound chats with a choice pay the lookup."""
    rid = (session.metadata or {}).get("requested_runner_id")
    if not rid:
        return None
    try:
        uuid.UUID(str(rid))
    except ValueError:
        return None
    return Runner.objects.filter(pk=rid).values_list("name", flat=True).first()


def mark_people_in(rows, viewer) -> list:
    """`rows`, each sensitive one told whether `viewer` is in it — one query for the
    page (`sensitivity.people_in`), not one per row in `session_out`."""
    rows = list(rows)
    sensitive = [s for s in rows if sensitivity.is_sensitive(s)]
    if sensitive:
        mine = sensitivity.people_in(sensitive, viewer)
        for s in sensitive:
            s._you_are_in_it = s.pk in mine
    return rows


def session_out(session: Session, *, reply: bool = False, viewer=None) -> dict:
    binding = getattr(session, "runner_binding", None)  # reverse 1:1 -> None when absent
    waiting_on_you = serializers.pending_menu(session) is not None
    last_reply, agent_spoke_last = transcript_read.last_reply_of(session, binding) if reply else ("", False)
    # The reply is whole (untrimmed), so carry it only where the feed renders it:
    # sessions where it is the person's turn. That is what bounds the payload.
    if not (agent_spoke_last or waiting_on_you):
        last_reply = ""
    runner = binding.runner if (binding and binding.runner_id) else None
    # What it is doing right now — the one resolver (`session_state`), which a
    # cloud session's in-flight turn answers as well as a laptop's engine flag.
    running = session_state.is_running(session, binding)
    turn_mode = getattr(session, "_turn_mode", None) or ""
    turn_origin = getattr(session, "_turn_origin", None) or ""
    # The supervisor feed's verdict for THIS caller — the one rule, which the
    # pushes ask too (canopy_sessions.feed). Only when the feed asked (`reply`).
    feed_status = (
        feed.status(
            viewer, session, waiting_on_you=waiting_on_you, agent_spoke_last=agent_spoke_last,
            running=running, auto=feed.ran_on_its_own(session),
            turn_open=session_state.turn_open(session),
        )
        if reply and viewer is not None
        else ""
    )
    # The name a human recognises for a runner-bound session is the emdash
    # task (what they see in emdash), not a thread_key hash a fallback title
    # may have captured. Web chats keep their own title. Web-origin sessions
    # also prefer their own title once set (e.g. the server-side auto-titler)
    # over a bound session_key, since a web chat's binding is an execution
    # detail, not the identity the human gave the conversation.
    #
    # A runner session keeps its own title too, unless that title is still the
    # thread_key fallback the rule above exists to hide. The key is only a better
    # name when it IS a name: a laptop's emdash task is, but a cloud runner's key
    # is a Claude session UUID, which would hide a real title ("Daily turn", set
    # by record-session) behind a UUID.
    is_fallback = (
        session.origin == Session.ORIGIN_RUNNER
        and binding is not None
        and session.title == binding.thread_key
    )
    prefer_own = bool(session.title) and not is_fallback
    sensitive = sensitivity.is_sensitive(session)
    return {
        "id": session.id,
        "agent_slug": session.agent.slug if session.agent_id else None,
        "project": session.project,
        "workspace": session.workspace_id,
        "title": (
            session.title
            if prefer_own
            else ((binding.session_key if (binding and binding.session_key) else "") or session.title)
        ),
        "status": session.status,
        "opening": transcript_read.opening_of(session),
        "origin_resource": session.origin_resource,
        "created_at": session.created_at,
        # When it last DID something (binding > newest message > created).
        "last_activity_at": transcript_read.last_activity_at(session, binding),
        # --- liveness: one shape, computed from the binding ---
        "origin": session.origin,
        "running": running,
        "runner_name": runner.name if runner else None,
        "runner_id": runner.id if runner else None,
        # The macOS user@host the live session belongs to — two laptop logins are
        # two runners told apart by this, so it names WHICH login on a shared box.
        "runner_host": binding.host if binding else "",
        "runner_location": runner.location if runner else None,
        "requested_runner_name": None if runner else _requested_runner_name(session),
        # The emdash project its worktree lives under: the binding's reported one,
        # else the session's own (repo for a project chat, agent slug for an agent
        # chat). What /ide groups by — `project` is blank on every agent chat.
        "emdash_project": (binding.emdash_project if binding else "") or session.emdash_project,
        # See SessionOut.runner_online: an embedder's delegated user cannot list
        # runners, so the session payload is where they learn their bound runner
        # went away. None when unbound — nothing to be offline.
        "runner_online": _runner_online(runner),
        "runner_status": _runner_status(runner),
        "runner_signed_out": _runner_signed_out(runner, session),
        # The model its runner's sessions start on (`opus[1m]`), as the box
        # reports it: how the chat's context meter sizes the window, since the
        # transcript never records the `[1m]`. "" when unbound or unreported.
        "runner_model": str((runner.capabilities or {}).get("claude_model") or "") if runner else "",
        "session_key": binding.session_key if binding else "",
        # The agent CLI this conversation runs on (`harness.clis`); "" until its first claim.
        "cli": session.cli,
        "transferring": staleness.is_transferring(binding),
        # Is this session blocked on a human? A bool on the LIST (the menu
        # itself rides the detail read) — a list carrying every session's full
        # dialog would pay for N sets of options to render one badge each.
        # Without it a waiting agent and an idle one look identical.
        "waiting_on_you": waiting_on_you,
        "close_error": binding.close_error if binding else "",
        "close_error_at": binding.close_error_at if binding else None,
        # Which of close_session's two branches a close takes — what a delete
        # confirm must name (an emdash task, or only this session).
        "runner_reported": control.is_runner_reported(binding),
        # Is a requested full-history ship still outstanding? Exact, so the
        # client need not guess: a fixed wait reads too early, and polling until
        # rows GROW never ends on an already-complete session. The runner clears
        # this flag when it ships the final chunk, so it answers "is anything
        # still coming?" directly instead of inferring it from an absence.
        "backfill_pending": bool(binding and binding.backfill_requested),
        "notify_every_completion": session.notify_every_completion,
        "sensitive": sensitive,
        # A list reads who is in which once per page (`mark_people_in`).
        "you_are_in_it": ((session._you_are_in_it if hasattr(session, "_you_are_in_it")
                           else sensitivity.is_conversation_person(session, user=viewer))
                          if sensitive else None),
        "started_by_name": _started_by_name(session),
        "started_by_you": viewer is not None and access.is_own_session(viewer, session),
        "runner_requirements": sorted(rr.requirements_of_session(session)),
        "last_reply": last_reply,
        "agent_spoke_last": agent_spoke_last,
        "turn_mode": turn_mode,
        "turn_origin": turn_origin,
        "turn_runner_name": getattr(session, "_turn_runner_name", None) or None,
        "feed_status": feed_status,
        "created_by": session.created_by.email if session.created_by_id else None,
        "provenance": provenance.public(session.provenance),
        "parent_turn_id": session.parent_turn_id,
        "parent_session_id": session.parent_session_id,
        "parent_task": session.parent_task,
        "parent_claude_session": session.parent_claude_session,
        "activity": session_activity.public(session.activity),
        # Where else the conversation is had (a Slack thread), from each channel's
        # own reader — canopy_sessions knows no channel (channel_bindings.py).
        **channel_bindings.of(session),
        # A private conversation is listed by what it is, never by what it says:
        # none of these is ever written with its words (`sensitivity.seal`), and a
        # list must not start reading them if that ever changes.
        **({"title": "", "opening": "", "last_reply": ""} if sensitive else {}),
    }


def _started_by_name(session: Session) -> str:
    """Who started it: a member's name, "a visitor" for a contact's; "" for a
    runner's or an agent's own. A name, never what they said."""
    if session.created_by_id:
        return control.person_name(session.created_by)
    return "a visitor" if session.contact_id else ""
