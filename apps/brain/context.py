"""What a learning run's turn is told about itself: the learner, the target, who it answers to, the run, and the
sessions due. Built by the learning system, never by the turn.

A run's turn is an ordinary full-profile turn of its operator agent (`runs.start`): its entry skill may do anything
the agent's turns may, under the operator's turn mode and rails. What makes it a learning run is this context, put in
two places from one function: the run's first prompt (`runs.prompt`) and the turn's caller envelope
(`harness.caller_context`, key `brain_run`, read with `who_is_asking`). The sessions due are named by source id and
counted, never quoted: their words are read only through `brain_read`, so every read is recorded and eligibility,
consent and the cascade apply. Everything the turn makes in canopy is recorded against what it read (`lineage`)."""
from __future__ import annotations

from .models import BRAIN_RUN_KEY, BrainRun

#: How many due source ids the envelope names at most; `sessions_due` is the whole count.
MAX_DUE_IDS = 200


def _name(u) -> str:
    return (u.get_full_name() or u.email or u.username) if u is not None else ""


def due(run: BrainRun) -> tuple[int, list[int]]:
    """How many sources the run may still read, and the first of them by id."""
    from .reading import candidates  # noqa: PLC0415 — reading reads this module's neighbours

    qs = candidates(run)
    return qs.count(), list(qs.values_list("pk", flat=True)[:MAX_DUE_IDS])


def block(run: BrainRun, *, accountable=None) -> dict:
    """The envelope's `brain_run`: everything the run is, by id and name, nothing a session said."""
    cap, t = run.capability, run.target
    count, ids = due(run) if run.state == BrainRun.OPEN else (0, [])
    if accountable is None and run.session_id is not None:
        from apps.harness.models import Turn  # noqa: PLC0415 — harness reads this app's neighbours

        first = (Turn.objects.filter(chat_session_id=run.session_id).order_by("created_at")
                 .select_related("initiator_user").first())
        accountable = getattr(first, "initiator_user", None)      # a system turn's accountable person
    return {
        "run": run.pk, "state": run.state, "learner": cap.slug, "learner_name": cap.name,
        "class": cap.purpose_class, "purpose": run.purpose, "entry": cap.entry, "output_kinds": list(cap.output_kinds or []),
        "subject_type": run.subject_type, "subject": run.subject, "scope": run.scope,
        "target": ({"id": t.pk, "kind": "project" if t.project_id else "workspace", "workspace": t.workspace_id,
                    "project": t.project.handle if t.project_id else "", "schedule_id": t.schedule_id}
                   if t is not None else None),
        "accountable": ({"email": accountable.email, "name": _name(accountable)} if accountable is not None else None),
        "sessions_due": count, "source_ids_due": ids,
        "how": ("Read the sessions due only with brain_read (each read is recorded, and only what everyone in a "
                "session agreed to is served). Write findings with brain_emit, citing the sources read. Anything "
                "else this turn makes in canopy is recorded against the sessions it read, so a removal can reach it."),
    }


def of_turn(turn) -> dict | None:
    """The `brain_run` block for a turn in a run's conversation, or None."""
    from apps.canopy_sessions import access  # noqa: PLC0415 — canopy_sessions reads this app's readers

    conversation = access.turn_conversation(turn)
    if not access.is_brain_conversation(conversation):
        return None
    run_id = (conversation.metadata or {}).get(BRAIN_RUN_KEY)
    run = (BrainRun.objects.select_related("capability", "target", "target__project").filter(pk=run_id).first()
           if str(run_id or "").isdecimal() else None)
    return block(run) if run is not None else None


def prompt_lines(run: BrainRun, *, accountable=None) -> list[str]:
    """The same context, in words, for the run's first prompt."""
    b = block(run, accountable=accountable)
    t = b["target"]
    where = (f"the project {t['project']} in {t['workspace']}" if t and t["kind"] == "project"
             else f"the workspace {t['workspace']} and the workspaces below it" if t else f"scope {run.scope or 'all'}")
    who = b["accountable"]["email"] if b["accountable"] else "nobody named"
    return [f"Learner: {b['learner']} ({b['class']}, purpose {b['purpose']}); it emits {', '.join(b['output_kinds']) or 'nothing'}.",
            f"Target: {where}." + (f" Its schedule is #{t['schedule_id']}." if t else ""),
            f"Accountable: {who}.",
            f"Sessions due: {b['sessions_due']}.",
            b["how"]]
