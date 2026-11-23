"""What a learning run's turn is told about itself: the learner, the target, who it answers to, the run, and the
sessions due. Built by the learning system, never by the turn.

A run's turn is an ordinary full-profile turn of its operator agent (`runs.start`): its entry skill may do anything
the agent's turns may, under the operator's turn mode and rails. What makes it a learning run is this context, put in
two places from one function: the run's first prompt (`runs.prompt`) and the turn's caller envelope
(`harness.caller_context`, key `brain_run`, read with `who_is_asking`). The sessions due are named by source id and
counted, never quoted: their words are read only through `brain_read`, so every read is recorded and eligibility,
consent and the cascade apply. Everything the turn makes in canopy is recorded against what it read (`lineage`).

It names the window the run reads in (`lookback_days`: sessions active in that many days, newest first) and the open
findings its learner already holds for this subject and workspace (`open_findings`, by id, key, title and sightings,
most recently seen first, capped; `findings.open_for`), so a run reuses a key for a problem already found instead of
minting a new one. A faded finding is not among them. And the ones dismissed within the cooldown (`dismissed_findings`,
most recently dismissed first, capped; `findings.dismissed_for`), so a run does not raise them again unless something
changed. Their titles are the learner's own words, already scrubbed.

The context is a typed, versioned document (`schemas.BrainRunContext`, `VERSION`): `document` builds it and checks it
against the type; `runs.start` keeps the one the run was started with (`BrainRun.context`), which the brain page shows
on the run. A new field is additive; a change in meaning is a new `VERSION`."""
from __future__ import annotations

from apps.canopy_sessions import access
from apps.harness.models import Turn

from . import budget, findings
from .models import BRAIN_RUN_KEY, BrainRun
from .reading import candidates, verdicts
from .schemas import BrainRunContext

#: How many due source ids the envelope names at most; `sessions_due` is the whole count.
MAX_DUE_IDS = 200
#: How many due sessions the context names with their triage verdict (`triage`), best first.
MAX_TRIAGE = 20
#: The context document's version: bump it when a field changes meaning, never for an added one.
VERSION = 1


def _name(u) -> str:
    return (u.get_full_name() or u.email or u.username) if u is not None else ""


def due(run: BrainRun) -> tuple[int, list[int]]:
    """How many sources the run may still read, and the first of them, best first (`triage`; past what it triaged,
    most recently active first)."""
    count, ids, _ = due_triaged(run)
    return count, ids


def due_triaged(run: BrainRun) -> tuple[int, list[int], list[dict]]:
    """`due`, and the first `MAX_TRIAGE` sessions' verdicts ({source, score, reason}), best first."""
    qs = candidates(run)
    found = verdicts(run, qs)
    ids = [v.source for v in found][:MAX_DUE_IDS]
    if len(ids) < MAX_DUE_IDS:
        ids += list(qs.exclude(pk__in=ids).values_list("pk", flat=True)[:MAX_DUE_IDS - len(ids)])
    return qs.count(), ids, [{"source": v.source, "score": v.score, "reason": v.reason} for v in found[:MAX_TRIAGE]]


def document(run: BrainRun, *, accountable=None) -> dict:
    """The run's context document, checked against its type (`schemas.BrainRunContext`)."""
    return BrainRunContext.model_validate(block(run, accountable=accountable)).model_dump()


def block(run: BrainRun, *, accountable=None) -> dict:
    """The envelope's `brain_run`: everything the run is, by id and name, nothing a session said."""
    cap, t = run.capability, run.target
    count, ids, triaged = due_triaged(run) if run.state == BrainRun.OPEN else (0, [], [])
    if accountable is None and run.session_id is not None:
        first = (Turn.objects.filter(chat_session_id=run.session_id).order_by("created_at")
                 .select_related("initiator_user").first())
        accountable = getattr(first, "initiator_user", None)      # a system turn's accountable person
    lim, left = budget.limits(t), (budget.run_left(run) if run.state == BrainRun.OPEN else {"sources": 0, "outputs": 0})
    total, known = findings.open_for(run, workspace=run.scope or None)
    gone_total, gone = findings.dismissed_for(run, workspace=run.scope or None)
    return {
        "version": VERSION,
        "run": run.pk, "state": run.state, "learner": cap.slug, "learner_name": cap.name,
        "purpose_class": cap.purpose_class, "purpose": run.purpose, "entry": cap.entry,
        "output_kinds": list(cap.output_kinds or []),
        "subject_type": run.subject_type, "subject": run.subject, "scope": run.scope,
        "target": ({"id": t.pk, "kind": "project" if t.project_id else "workspace", "workspace": t.workspace_id,
                    "project": t.project.handle if t.project_id else "", "schedule_id": t.schedule_id}
                   if t is not None else None),
        "accountable": ({"email": accountable.email, "name": _name(accountable)} if accountable is not None else None),
        "sessions_due": count, "source_ids_due": ids, "triage": triaged, "lookback_days": budget.lookback_days(t),
        "open_findings_total": total, "open_findings": known,
        "dismissed_findings_total": gone_total, "dismissed_findings": gone,
        "budget": {"max_sources_per_run": lim.max_sources_per_run, "max_outputs_per_run": lim.max_outputs_per_run,
                   "sources_left": left["sources"], "outputs_left": left["outputs"]},
        "how": ("Read the sessions due only with brain_read (each read is recorded, and only what everyone in a "
                "session agreed to is served). Write findings with brain_emit, citing the sources read, with a "
                "`key` naming what it is so a repeat folds into the open finding. Before you emit, check the open "
                "findings: when one is the same problem, emit with its key (or `same_as` its id) so it folds (more "
                "evidence, seen + 1); mint a new key only for a problem none of them is, and never two findings for "
                "one problem in a run. Do not raise a dismissed finding again unless something changed (a repeat "
                "of its key folds into it and stays dismissed). Every piece of evidence is {source, index, quote}: a "
                "verbatim quote of a message you read, or the emit is refused. Stop at the budget. Anything else this turn makes in canopy is recorded "
                "against the sessions it read, so a removal can reach it."),
    }


def of_turn(turn) -> dict | None:
    """The `brain_run` block for a turn in a run's conversation, or None."""
    conversation = access.turn_conversation(turn)
    if not access.is_brain_conversation(conversation):
        return None
    run_id = (conversation.metadata or {}).get(BRAIN_RUN_KEY)
    run = (BrainRun.objects.select_related("capability", "target", "target__project").filter(pk=run_id).first()
           if str(run_id or "").isdecimal() else None)
    return document(run) if run is not None else None


def prompt_lines(run: BrainRun, *, accountable=None) -> list[str]:
    """The same context, in words, for the run's first prompt."""
    b = document(run, accountable=accountable)
    t = b["target"]
    where = (f"the project {t['project']} in {t['workspace']}" if t and t["kind"] == "project"
             else f"the workspace {t['workspace']} and the workspaces below it" if t else f"scope {run.scope or 'all'}")
    who = b["accountable"]["email"] if b["accountable"] else "nobody named"
    return [f"Learner: {b['learner']} ({b['purpose_class']}, purpose {b['purpose']}); it emits {', '.join(b['output_kinds']) or 'nothing'}.",
            f"Target: {where}." + (f" Its schedule is #{t['schedule_id']}." if t else ""),
            f"Accountable: {who}.",
            f"Sessions due: {b['sessions_due']}"
            + (f", active in the last {b['lookback_days']} days" if b["lookback_days"] else "")
            + (", most worth reading first (brain_read serves them in this order; pass `source` to read any due one):"
               if b["triage"] else ", most recent first."),
            *_triage_lines(b),
            *_finding_lines(b),
            f"Budget: at most {b['budget']['max_sources_per_run']} sessions read and "
            f"{b['budget']['max_outputs_per_run']} outputs written this run.",
            b["how"]]


def _triage_lines(b: dict) -> list[str]:
    """One line per triaged session: its id, score and why (counts only, never its words)."""
    return [f"- source {v['source']} (score {v['score']}): {v['reason']}" for v in b["triage"]]


def _finding_lines(b: dict) -> list[str]:
    return [*_open_lines(b), *_dismissed_lines(b)]


def _open_lines(b: dict) -> list[str]:
    known = b["open_findings"]
    if not known:
        return ["Open findings already held for this subject: none."]
    more = b["open_findings_total"] - len(known)
    head = (f"Open findings already held for this subject ({b['open_findings_total']}, most recently seen first"
            + (f"; the {more} oldest are not listed" if more > 0 else "") + "); reuse a key when it is the same problem:")
    return [head, *(f"- #{f['id']} key `{f['key'] or '(none)'}`: {f['title']} (seen {f['seen']}, last "
                     f"{f['last_seen_at'][:10]})" for f in known)]


def _dismissed_lines(b: dict) -> list[str]:
    gone = b.get("dismissed_findings") or []
    if not gone:
        return []
    more = b["dismissed_findings_total"] - len(gone)
    head = (f"Dismissed lately ({b['dismissed_findings_total']}, most recently dismissed first"
            + (f"; the {more} oldest are not listed" if more > 0 else "")
            + "), dismissed — don't re-raise unless something changed:")
    return [head, *(f"- #{f['id']} key `{f['key']}`: {f['title']} (dismissed {f['dismissed_at'][:10]})" for f in gone)]
