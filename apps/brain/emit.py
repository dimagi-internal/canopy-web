"""What a brain run writes (`brain_emit`): an output that carries the lineage of what it actually read.

An output is about the run's own subject (never one it names), of a kind its feature declares, and cites the
sources it came from. Every cited source must be one THIS run read (`BrainRunRead`) and must still be eligible
now: a source cited but never read, or one whose people withdrew since, is refused whole. The lineage
(`OutputLineage`) is written from the reads, so a later removal can find every output a source fed. The words
are scrubbed like every learning read, keys as well as values. Emitting the same thing twice is one output."""
from __future__ import annotations

import hashlib
import json

from django.db import transaction

from apps.agents.models import AgentProject
from apps.common import redact

from . import associate, learning_reads
from .eligibility import eligible
from .models import BrainOutput, BrainRun, BrainRunRead, BrainSource, OutputLineage, SourceLink

MAX_BODY_CHARS, MAX_SOURCES = 20_000, 50


class EmitRefusedError(ValueError):
    """The output asked for may not be written (a 422 at the route), with the reason in words."""


#: The output kind of a project judge: not a `BrainOutput` but a `SourceLink` (layer 3 of `associate`).
LINK = "link"
LINK_TIERS = ("core", "adjacent", "candidate", "rejected")


def emit(run: BrainRun, *, kind: str, title: str, body: dict, source_ids, turn_id: str = ""):
    cap = run.capability
    if learning_reads.run_refusal(cap, run.subject, run.scope) or not cap.enabled or run.state != BrainRun.OPEN:
        raise EmitRefusedError("this run may not emit")
    if kind not in (cap.output_kinds or []):
        raise EmitRefusedError(f"{cap.slug} emits {', '.join(cap.output_kinds) or 'nothing'}, not {kind!r}")
    if kind == LINK:
        return _emit_link(run, body=body, source_ids=source_ids)
    if not isinstance(body, dict) or len(json.dumps(body, default=str)) > MAX_BODY_CHARS:
        raise EmitRefusedError(f"body must be an object of at most {MAX_BODY_CHARS} characters")
    ids = sorted({int(i) for i in source_ids})
    if not ids or len(ids) > MAX_SOURCES:
        raise EmitRefusedError(f"cite between 1 and {MAX_SOURCES} sources")
    read = set(BrainRunRead.objects.filter(run=run, source_id__in=ids).values_list("source_id", flat=True))
    if set(ids) - read:
        raise EmitRefusedError(f"cites a source this run did not read: {sorted(set(ids) - read)}")
    sources = list(BrainSource.objects.filter(pk__in=ids).select_related("session"))
    subject = int(run.subject) if cap.subject_type == "person" else None
    for s in sources:
        if not eligible(s, cap, run.purpose, subject):
            raise EmitRefusedError(f"source {s.pk} is no longer open to this feature")
    workspaces = {s.workspace_id for s in sources}
    if run.scope:
        # A run started for a workspace reads only its subtree and what it learns is that workspace's: its owners
        # and their ancestors' see it (`outputs.visible_to`).
        workspaces = {run.scope}
    elif len(workspaces) != 1:
        raise EmitRefusedError("an output cites sources of one workspace; emit one per workspace")
    scrubbed = _scrub(body)
    clean_title = redact.redact_text(title or "")[0][:300]
    fingerprint = hashlib.sha256(json.dumps([kind, clean_title, scrubbed, ids], sort_keys=True,
                                            default=str).encode()).hexdigest()
    with transaction.atomic():
        existing = BrainOutput.objects.filter(run=run, fingerprint=fingerprint).first()
        if existing is not None:
            return existing
        out = BrainOutput.objects.create(
            capability=cap, kind=kind, workspace_id=workspaces.pop(), subject_type=run.subject_type,
            subject=run.subject, title=clean_title, body=scrubbed, turn_id=turn_id, run=run,
            fingerprint=fingerprint)
        for s in sources:
            people = sorted(s.participants.filter(user__isnull=False).values_list("user_id", flat=True))
            OutputLineage.objects.create(output_kind="brain.output", output_id=str(out.pk), source=s,
                                         participant_scope=people)
    return out


def _emit_link(run: BrainRun, *, body: dict, source_ids):
    """A judge's ruling: which tier one session belongs to for the run's project, with a confidence and the
    model that judged. It must be a session this run read and one still open to it, and it never overrides
    what outranks a judgment (a person's call, a dispatch): `associate.put` decides."""
    if run.subject_type != "project":
        raise EmitRefusedError("a link is a ruling about one project")
    if not isinstance(body, dict):
        raise EmitRefusedError("a link's body is an object: {tier, confidence, model, evidence}")
    ids = sorted({int(i) for i in source_ids})
    if len(ids) != 1:
        raise EmitRefusedError("a link rules on exactly one source")
    if not BrainRunRead.objects.filter(run=run, source_id=ids[0]).exists():
        raise EmitRefusedError(f"cites a source this run did not read: {ids}")
    tier, confidence, model = body.get("tier"), body.get("confidence"), str(body.get("model") or "")[:64]
    if tier not in LINK_TIERS:
        raise EmitRefusedError(f"tier must be one of {', '.join(LINK_TIERS)}")
    if not isinstance(confidence, int | float) or isinstance(confidence, bool) or not 0 <= confidence <= 1:
        raise EmitRefusedError("confidence is a number from 0 to 1")
    if not model:
        raise EmitRefusedError("name the model that judged")
    source = BrainSource.objects.select_related("session").get(pk=ids[0])
    if not eligible(source, run.capability, run.purpose):
        raise EmitRefusedError(f"source {source.pk} is no longer open to this feature")
    project = AgentProject.objects.filter(pk=run.subject, workspace_id=source.workspace_id).first()
    if project is None or project.is_personal:
        raise EmitRefusedError("a link rules on a workspace project of the source's own workspace")
    evidence = body.get("evidence") or []
    if not isinstance(evidence, list):
        raise EmitRefusedError("evidence is a list of short strings")
    evidence = [redact.redact_text(str(e))[0][:200] for e in evidence[:5]]
    return associate.put(source, project, how=SourceLink.JUDGED, tier=tier, evidence=evidence,
                         confidence=float(confidence), model=model)


def _scrub(value):
    """`common.redact` over a JSON value, its object KEYS too: a secret written as a key is still a secret."""
    if isinstance(value, dict):
        return {redact.redact_text(str(k))[0]: _scrub(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub(v) for v in value]
    return redact._redact_value(value)[0]
