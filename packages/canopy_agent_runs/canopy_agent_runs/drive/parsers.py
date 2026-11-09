"""Parsers for ACE's Drive run-folder YAML — ported from ace-web's
`apps/opps/parsers.py` (dataclasses) + the pure parsing functions in
`apps/opps/sync.py`.

These read the on-Drive ACE layout and produce *intermediate* dataclasses
(`StepManifest`, `JudgeVerdict`, `QAResult`, `Decision`, `ArtifactRef`,
`StepSnapshot`). A later stage maps those intermediates onto the
storage-agnostic read model in `apps.agent_runs.schemas` (Step / Verdict /
Decision / Artifact). Keeping the parse layer as plain dataclasses means the
parity tests can pin ACE's exact field-name/version tolerance without dragging
Pydantic strictness through the messy real-world YAML.

Pure Python + PyYAML only — no Django, no Drive client, no product-app import
(FRAMEWORK tier). The file-walking loaders (which take a DriveClient and read
verdict/decision files off a tree) land in the next stage on top of these.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

import yaml

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Intermediate dataclasses (ported from ace-web apps/opps/parsers.py + sync.py)
# ---------------------------------------------------------------------------
@dataclass
class OppManifest:
    slug: str
    display_name: str
    created_at: str | None = None
    created_by: str | None = None
    labels: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    current_run_id: str | None = None


@dataclass
class StepManifest:
    skill_name: str
    phase: str
    ordinal: int
    # pending | running | complete | qa-failed | error | skipped
    status: str
    started_at: str | None = None
    completed_at: str | None = None
    error: str | None = None
    preview_stats: dict = field(default_factory=dict)


@dataclass
class JudgeVerdict:
    score: float | None
    passed: bool | None
    evaluated_at: str | None
    criteria: dict = field(default_factory=dict)
    rationale: str = ""


@dataclass
class QAFailure:
    """One failed structural QA check. Mirrors ACE's ``lib/qa-types.ts``:
    every failure is severity=blocker and carries an ``auto_fix_hint`` the
    orchestrator passes to the producer for regeneration."""

    check: str
    type: str  # static | llm
    detail: str
    auto_fix_hint: str


@dataclass
class QAResult:
    """Structural QA verdict on a producer artifact. Distinct from
    ``JudgeVerdict``: QA is binary (pass / fail / incomplete) and gates eval.
    If QA fails irrecoverably the eval is skipped and the JudgeVerdict is
    absent / 'incomplete'."""

    skill: str  # the QA skill that produced this (e.g. "idea-to-pdd-qa")
    target_skill: str  # the producer skill being checked (e.g. "idea-to-pdd")
    verdict: str  # pass | fail | incomplete
    ran_at: str | None = None
    capture_path: str | None = None
    checks_run: int = 0
    checks_passed: int = 0
    checks_failed: int = 0
    failures: list[QAFailure] = field(default_factory=list)
    auto_fix_attempted: bool | None = None
    auto_fix_attempts: int | None = None
    auto_fix_succeeded: bool | None = None


@dataclass
class Decision:
    """One row from the per-run decisions log. Mirrors ACE's
    ``lib/decisions-schema.ts``. Each row records a load-bearing default a
    phase skill applied — what was asked, what was picked, what alternatives
    were on the table, and whether a human reviewer overrode the default."""

    id: str
    phase: str
    skill: str
    question: str
    ai_default: str
    override: str = ""
    options_considered: list[str] = field(default_factory=list)
    source: str = ""
    status: str = "ai-default"  # ai-default | overridden
    notes: str = ""
    override_reasoning: str = ""
    # v4 fields. evidence_basis: stated | inferred |
    # conflicting. conflict_signals: each competing source reading, populated
    # (>=2) only when conflicting. Legacy rows default evidence_basis="stated".
    evidence_basis: str = "stated"
    conflict_signals: list[str] = field(default_factory=list)


@dataclass
class ArtifactRef:
    name: str
    drive_file_id: str
    drive_web_link: str
    size_bytes: int | None
    mime_type: str
    path: str  # relative to the step's output/ folder, e.g. "pdd.md"


@dataclass
class StepSnapshot:
    step: StepManifest
    judge: JudgeVerdict | None
    artifacts: list[ArtifactRef]
    folder_id: str
    qa_result: QAResult | None = None


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
# Files at the run-folder root the artifact manifest attributes to a specific
# skill but that are shared substrates appended to by many skills. Their
# presence is NOT evidence the attributed skill ran (a fork carries them
# verbatim), so they are excluded from the artifact-presence completeness check.
_SHARED_SUBSTRATE_FILES = frozenset({"decisions.yaml", "decisions.yml"})

# Map raw run_state.yaml status strings to the canonical StepManifest status
# set (pending | running | complete | qa-failed | error | skipped). Unknown
# strings fall back to artifact presence rather than crashing.
_RUN_STATE_TO_CANONICAL: dict[str, str] = {
    "done": "complete",
    "complete": "complete",
    "running": "running",
    "in_progress": "running",
    "in-progress": "running",
    "failed": "error",
    "error": "error",
    "skipped": "skipped",
    "no-op": "skipped",
    "noop": "skipped",
    "pending": "pending",
}


# ---------------------------------------------------------------------------
# Decisions log parsing
# ---------------------------------------------------------------------------
def _extract_decision_rows(data: dict) -> list:
    """Pull the decision-row list out of a parsed decisions.yaml dict.

    Canonical v3 shape uses ``decisions:`` as the top-level key. Defensive
    fallback to ``rows:`` (with a warning) — when the typed
    ``decisions_append_rows`` MCP atom isn't reachable, phase subagents fall
    back to a direct file write and copy the SKILL.md example's ``rows:``
    parameter name as the YAML key. We accept that shape so legacy malformed
    files still render, instead of silently returning 0 rows. Returns ``[]``
    when neither key resolves to a list.
    """
    canonical = data.get("decisions")
    if canonical is None and isinstance(data.get("rows"), list):
        log.warning(
            "decisions.yaml uses legacy top-level key `rows:` instead of "
            "canonical `decisions:` — most likely written by a phase subagent "
            "that fell back from the typed `decisions_append_rows` MCP atom "
            "to a direct file write. Rendering the rows for back-compat; "
            "future writes should go through the atom (see ace#529 for the "
            "registration fix).",
        )
        canonical = data["rows"]
    if not isinstance(canonical, list):
        return []
    return canonical


def _parse_decision_rows(raw_rows: list) -> list[Decision]:
    """Convert raw decisions.yaml rows into Decision dataclasses.

    Reads v3 fields first (``options``, ``reasoning``); falls back to v2
    (``options_considered``, ``notes``). ``ai-default`` (v2/v3) falls back to
    ``default`` (v1). Old status values (``applied``, ``open``) map to
    ``ai-default``.

    Emits one ``warning`` per row that has an ``id`` but is missing
    ``question`` or ``ai-default`` — the schema-drift regression signature.
    """
    out: list[Decision] = []
    for row in raw_rows:
        if not isinstance(row, dict):
            continue
        rid = str(row.get("id") or "").strip()
        if not rid:
            continue
        opts_raw = row.get("options")
        if opts_raw is None:
            opts_raw = row.get("options_considered") or []
        ai_default = str(row.get("ai-default") or row.get("default") or "").strip()
        override = str(row.get("override") or "").strip()
        raw_status = str(row.get("status") or "ai-default").strip().lower()
        status = raw_status if raw_status == "overridden" else "ai-default"
        question = str(row.get("question") or "").strip()
        reasoning = str(row.get("reasoning") or row.get("notes") or "").strip()
        # Override-reasoning: underscore (ACE v3) or hyphen (hand-edited).
        override_reasoning = str(
            row.get("override_reasoning") or row.get("override-reasoning") or ""
        ).strip()
        # v4: evidence_basis is a closed enum — normalize case, fall back to
        # "stated" for legacy rows or any out-of-enum value.
        evidence_basis = str(row.get("evidence_basis") or "").strip().lower()
        if evidence_basis not in ("stated", "inferred", "conflicting"):
            evidence_basis = "stated"
        signals_raw = row.get("conflict_signals") or []
        conflict_signals = (
            [str(s) for s in signals_raw] if isinstance(signals_raw, list) else []
        )

        if not question or not ai_default:
            log.warning(
                "decisions.yaml row %r is missing %s — likely written against a "
                "stale schema (expected v3 fields: question, ai-default, options, "
                "reasoning). Row keys present: %s",
                rid,
                ", ".join(
                    name
                    for name, val in (("question", question), ("ai-default", ai_default))
                    if not val
                ),
                sorted(row.keys()),
            )

        out.append(
            Decision(
                id=rid,
                phase=str(row.get("phase") or "").strip(),
                skill=str(row.get("skill") or "").strip(),
                question=question,
                ai_default=ai_default,
                override=override,
                options_considered=(
                    [str(o) for o in opts_raw] if isinstance(opts_raw, list) else []
                ),
                source=str(row.get("source") or "").strip(),
                status=status,
                notes=reasoning,
                override_reasoning=override_reasoning,
                evidence_basis=evidence_basis,
                conflict_signals=conflict_signals,
            )
        )
    return out


def parse_decisions_yaml(body: str) -> list[Decision]:
    """Parse a full decisions.yaml body into Decision rows. Returns [] for
    empty/unparseable/non-dict bodies. Convenience over
    `_extract_decision_rows` + `_parse_decision_rows`."""
    try:
        data = yaml.safe_load(body) or {}
    except yaml.YAMLError:
        return []
    if not isinstance(data, dict):
        return []
    return _parse_decision_rows(_extract_decision_rows(data))


# ---------------------------------------------------------------------------
# Verdict (JudgeVerdict) parsing
# ---------------------------------------------------------------------------
_SCALE_RE = re.compile(r"^\s*0\s*-\s*(\d+(?:\.\d+)?)\s*$")


def _detect_score_scale(data: dict) -> float | None:
    """Pull the highest declared ``scale: "0-N"`` from a verdict YAML.

    Walks ``dimensions`` and any top-level ``scale`` field. Returns the upper
    bound ``N`` as a float, or None if no explicit scale is present.
    """
    candidates: list[float] = []

    def _consume(value):
        if isinstance(value, str):
            m = _SCALE_RE.match(value)
            if m:
                try:
                    candidates.append(float(m.group(1)))
                except ValueError:
                    pass
        elif isinstance(value, (int, float)):
            candidates.append(float(value))

    _consume(data.get("scale"))

    dims = data.get("dimensions")
    if isinstance(dims, dict):
        for v in dims.values():
            if isinstance(v, dict):
                _consume(v.get("scale"))

    if not candidates:
        return None
    return max(candidates)


def _parse_verdict_yaml(body: str) -> JudgeVerdict | None:
    """Parse a verdict YAML body into a JudgeVerdict.

    Tolerant of both the old short shape ({score, passed, ...}) and the
    plugin's current eval shape ({overall_score, verdict, dimensions, ...}).
    Score is normalized to 0-100 at parse time using an explicit
    ``dimensions.<key>.scale: "0-N"`` when declared, else a magnitude
    heuristic (correct for 0-10 / 0-100, hence the explicit-scale preference).
    """
    try:
        data = yaml.safe_load(body) or {}
    except yaml.YAMLError:
        return None
    if not isinstance(data, dict):
        return None

    score_raw = data.get("score")
    if score_raw is None:
        score_raw = data.get("overall_score")
    try:
        score = float(score_raw) if score_raw is not None else None
    except (TypeError, ValueError):
        score = None

    if score is not None:
        scale_max = _detect_score_scale(data)
        if scale_max is not None and scale_max > 0:
            score = (score / scale_max) * 100.0

    passed_raw = data.get("passed")
    if isinstance(passed_raw, bool):
        passed: bool | None = passed_raw
    else:
        verdict = str(data.get("verdict") or data.get("gate") or "").lower()
        if verdict in ("pass", "approved"):
            passed = True
        elif verdict in ("fail", "rejected"):
            passed = False
        else:
            passed = None

    evaluated_at = (
        data.get("evaluated_at") or data.get("ran_at") or data.get("timestamp")
    )

    criteria_raw = data.get("criteria") or data.get("dimensions") or {}
    criteria = criteria_raw if isinstance(criteria_raw, dict) else {}

    rationale = data.get("rationale") or data.get("summary") or ""

    return JudgeVerdict(
        score=score,
        passed=passed,
        evaluated_at=evaluated_at,
        criteria=criteria,
        rationale=str(rationale),
    )


def _skill_from_verdict_stem(stem: str) -> str:
    """Derive a skill name from an old-layout verdict filename stem, e.g.
    "ocs-chatbot-eval-deep" -> "ocs-chatbot-eval", "opp-eval-deep" ->
    "opp-eval", "idea-to-pdd" -> "idea-to-pdd"."""
    for suffix in ("-quick", "-deep", "-monitor", "-shallow"):
        if stem.endswith(suffix):
            return stem[: -len(suffix)]
    return stem


def _skill_from_verdict_producer(
    producer: str, registered_skills: set[str]
) -> str | None:
    """Derive the target lifecycle skill for a new-layout verdict-file
    producer. Eval-suffix (`<target>-eval` evaluates `<target>`) or self-eval
    (the producer IS the row). Returns None when neither candidate matches a
    known skill (drop the verdict rather than attach to a phantom row)."""
    if producer in registered_skills:
        return producer
    if producer.endswith("-eval"):
        trimmed = producer[: -len("-eval")]
        if trimmed in registered_skills:
            return trimmed
    return None


# ---------------------------------------------------------------------------
# QA result parsing
# ---------------------------------------------------------------------------
def _parse_qa_result_yaml(body: str, qa_skill: str) -> QAResult | None:
    """Parse a QA result YAML body into a ``QAResult``. Schema canonical at
    ACE's ``lib/qa-types.ts``. The target lifecycle skill is the QA skill name
    with the ``-qa`` suffix stripped. Returns None when the verdict tier is
    not pass/fail/incomplete or the body isn't a dict."""
    try:
        data = yaml.safe_load(body) or {}
    except yaml.YAMLError:
        return None
    if not isinstance(data, dict):
        return None

    verdict = str(data.get("verdict") or "").lower()
    if verdict not in ("pass", "fail", "incomplete"):
        return None

    target_skill = qa_skill[: -len("-qa")] if qa_skill.endswith("-qa") else qa_skill

    failures: list[QAFailure] = []
    raw_failures = data.get("failures") or []
    if isinstance(raw_failures, list):
        for entry in raw_failures:
            if not isinstance(entry, dict):
                continue
            failures.append(
                QAFailure(
                    check=str(entry.get("check") or ""),
                    type=str(entry.get("type") or "static"),
                    detail=str(entry.get("detail") or ""),
                    auto_fix_hint=str(entry.get("auto_fix_hint") or ""),
                )
            )

    stats = data.get("stats") or {}
    auto_fix = data.get("auto_fix") or {}

    return QAResult(
        skill=qa_skill,
        target_skill=target_skill,
        verdict=verdict,
        ran_at=str(data.get("ran_at")) if data.get("ran_at") else None,
        capture_path=str(data.get("capture_path")) if data.get("capture_path") else None,
        checks_run=int(stats.get("checks_run") or 0) if isinstance(stats, dict) else 0,
        checks_passed=int(stats.get("checks_passed") or 0) if isinstance(stats, dict) else 0,
        checks_failed=int(stats.get("checks_failed") or 0) if isinstance(stats, dict) else 0,
        failures=failures,
        auto_fix_attempted=auto_fix.get("attempted") if isinstance(auto_fix, dict) else None,
        auto_fix_attempts=auto_fix.get("attempts") if isinstance(auto_fix, dict) else None,
        auto_fix_succeeded=auto_fix.get("succeeded") if isinstance(auto_fix, dict) else None,
    )


# ---------------------------------------------------------------------------
# run_state.yaml step-status extraction + step building
# ---------------------------------------------------------------------------
def _extract_step_statuses(state_data: dict | None) -> dict[str, str]:
    """Pull per-skill ``status:`` strings out of a parsed run_state.yaml.

    Handles three phase shapes:
      A — explicit ``steps:`` wrapper (current plugin):
            phases: {commcare-setup: {status: running, steps: {pdd-to-learn-app: {status: done}}}}
      B — bare skill->status mapping (older plugin):
            phases: {idea-to-design: {idea-to-pdd: done}}
      C — ``steps:`` wrapper with no phase-level ``status:`` (same branch as A).

    Unknown / malformed phase entries are skipped silently (read-side code).
    """
    if not isinstance(state_data, dict):
        return {}
    phases = state_data.get("phases")
    if not isinstance(phases, dict):
        return {}
    out: dict[str, str] = {}
    for phase_value in phases.values():
        if not isinstance(phase_value, dict):
            continue
        steps_map = phase_value.get("steps") if "steps" in phase_value else phase_value
        if not isinstance(steps_map, dict):
            continue
        for skill_name, step_value in steps_map.items():
            if not isinstance(skill_name, str):
                continue
            if isinstance(step_value, str):
                out[skill_name] = step_value
            elif isinstance(step_value, dict):
                status = step_value.get("status")
                if isinstance(status, str):
                    out[skill_name] = status
    return out


def _build_steps(
    skill_registry,
    artifacts_by_skill: dict[str, list[ArtifactRef]],
    verdicts_by_skill: dict[str, JudgeVerdict],
    folder_id: str,
    qa_results_by_skill: dict[str, QAResult] | None = None,
    step_status_by_skill: dict[str, str] | None = None,
) -> list[StepSnapshot]:
    """Synthesize StepSnapshot rows from the skill registry + Drive data.

    ``skill_registry`` is any iterable of objects with ``.name`` / ``.phase``
    / ``.ordinal`` attributes (the live ACE registry, or a test stub).

    Step status precedence (highest -> lowest):
      1. ``qa-failed`` — irrecoverable QA verdict, regardless of run_state.
      2. ``step_status_by_skill`` — declared status in run_state.yaml,
         normalized via ``_RUN_STATE_TO_CANONICAL`` (primary source of truth;
         carries semantics artifact-presence can't: running, skipped/no-op).
      3. Artifact presence — fallback for legacy runs / tests with no
         run_state. Shared-substrate files (decisions.yaml) don't count.
    """
    qa_results_by_skill = qa_results_by_skill or {}
    step_status_by_skill = step_status_by_skill or {}
    steps: list[StepSnapshot] = []
    for skill_meta in skill_registry:
        artifacts = artifacts_by_skill.get(skill_meta.name, [])
        qa_result = qa_results_by_skill.get(skill_meta.name)
        load_bearing_artifacts = [
            a for a in artifacts
            if getattr(a, "name", None) not in _SHARED_SUBSTRATE_FILES
        ]

        if qa_result is not None and qa_result.verdict == "fail":
            status = "qa-failed"
        else:
            declared = step_status_by_skill.get(skill_meta.name)
            normalized = _RUN_STATE_TO_CANONICAL.get(declared) if declared else None
            if normalized is not None:
                status = normalized
                if status == "complete" and not load_bearing_artifacts:
                    log.debug(
                        "step %s/%s declared complete in run_state.yaml but no "
                        "load-bearing artifacts present (likely a no-op step)",
                        skill_meta.phase, skill_meta.name,
                    )
            elif load_bearing_artifacts:
                status = "complete"
            else:
                status = "pending"

        step_manifest = StepManifest(
            skill_name=skill_meta.name,
            phase=skill_meta.phase,
            ordinal=skill_meta.ordinal,
            status=status,
        )
        steps.append(
            StepSnapshot(
                step=step_manifest,
                judge=verdicts_by_skill.get(skill_meta.name),
                artifacts=artifacts,
                folder_id=folder_id,
                qa_result=qa_result,
            )
        )
    return steps
