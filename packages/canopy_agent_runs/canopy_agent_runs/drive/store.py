"""`DriveRunStore` — a `RunStore` backed by ACE-shaped Drive run-folders.

This is the Drive half of the Phase-1 contract: it reads an agent's Drive
run-folder tree and returns the *same* storage-agnostic read model
(`canopy_agent_runs.schemas`) the DB adapter (`DbRunStore`) returns, so the two
are interchangeable behind the `RunStore` Protocol.

It ports the ORCHESTRATION from ace-web's ``apps/opps/sync.py`` —
``_load_opp_run`` in particular:

    read run_state.yaml
      → recursively list the run folder
      → attribute files to skills/steps via the artifact manifest
      → parse judge verdicts + QA results + the decisions log
      → synthesize ordered steps (status from run_state, falling back to
        artifact presence)
      → derive run status from the steps map

What's deliberately deferred (noted, not ported):

* The snapshot-cache / Drive-Changes freshness-overlay machinery
  (``apps/opps/freshness_overlays.py``, the OppCard cold-load cache). We read
  straight from Drive on every call; caching lands in the REST phase.

``fork`` is implemented (ported from ace-web ``apps/opps/opp_forker.py``): it
mints a new run-id folder under the same agent root, copies the kept (pre-fork)
phase subtrees + run-root inputs, rewrites ``decisions.yaml`` per the shared
``FORK_MODES`` + ``edits`` contract, and synthesizes a fresh ``run_state.yaml``.

The artifact/skill manifest is PLUGGABLE (constructor args), not hard-coded to
ACE's skills. ``DEFAULT_MANIFEST`` / ``DEFAULT_SKILL_REGISTRY`` are a small
ported stub (the shape of ``lib/artifact-manifest.ts``) so the store works
out of the box; a real deploy injects the live registry.

FRAMEWORK tier: pure Python + PyYAML + the local DriveClient Protocol. No
Django, no Google SDK, no product-app import.
"""
from __future__ import annotations

import datetime as dt
import logging
import re
from dataclasses import dataclass
from typing import Any

import yaml

from ..schemas import (
    Artifact,
    Decision,
    Gate,
    Run,
    RunSummary,
    Step,
    Verdict,
    derive_status,
)
from ..stores import FORK_MODES, _apply_decision_edit
from .client import DriveClient, DriveFile
from .parsers import (
    ArtifactRef,
    JudgeVerdict,
    QAResult,
    StepSnapshot,
    _build_steps,
    _extract_step_statuses,
    _parse_qa_result_yaml,
    _parse_verdict_yaml,
    _skill_from_verdict_producer,
    _skill_from_verdict_stem,
    parse_decisions_yaml,
)

log = logging.getLogger(__name__)

FOLDER_MIME = "application/vnd.google-apps.folder"


# ---------------------------------------------------------------------------
# Pluggable manifest + skill registry (ported stub of lib/artifact-manifest.ts)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class SkillMeta:
    """One row of the skill registry — defines a step's identity + order.

    ``name`` is the stable step key (the skill slug); ``phase`` groups it;
    ``ordinal`` orders it within the run. The store iterates the registry to
    synthesize the ordered step list, exactly as ACE's ``SKILL_REGISTRY`` does.
    """

    name: str
    phase: str
    ordinal: int
    title: str = ""


# A manifest entry maps a file path (under the run root) to its producing
# skill + phase — the dict shape ACE's ``ARTIFACT_MANIFEST`` decodes to.
# ``path`` may carry ``YYYY-MM-DD`` placeholders (matched as a date literal).
ManifestEntry = dict[str, str]

# A tiny default so the store is usable without injection. A real deploy
# passes its own (loaded from the agent's plugin manifest).
DEFAULT_SKILL_REGISTRY: tuple[SkillMeta, ...] = (
    SkillMeta("idea-to-pdd", "1-design", 1),
    SkillMeta("pdd-to-learn-app", "2-build", 2),
    SkillMeta("pdd-to-deliver-app", "2-build", 3),
    SkillMeta("app-test", "2-build", 4),
)

DEFAULT_MANIFEST: tuple[ManifestEntry, ...] = (
    {"path": "idea.md", "produced_by": "external", "phase": "1-design"},
    {"path": "1-design/pdd.md", "produced_by": "idea-to-pdd", "phase": "1-design"},
    {"path": "pdd.md", "produced_by": "idea-to-pdd", "phase": "1-design"},
)

# Run-root files carried VERBATIM into a fork's new run folder (alongside the
# kept phase subtrees). They describe the source pack the kept phases worked
# from. ``decisions.yaml`` is NOT here — it gets a trim/mode/edit rewrite, not a
# straight copy (see ``DriveRunStore.fork``). Mirrors ACE's
# ``opp_forker._RUN_ROOT_FILES_TO_COPY``.
_FORK_RUN_ROOT_FILES = ("idea.md", "inputs-manifest.yaml")


# ---------------------------------------------------------------------------
# Drive-tree helpers (ported from sync.py)
# ---------------------------------------------------------------------------
def _is_folder(f: DriveFile) -> bool:
    return f.mime_type == FOLDER_MIME


def _find_child(files: list[DriveFile], name: str) -> DriveFile | None:
    for f in files:
        if f.name == name:
            return f
    return None


def _find_child_folder(files: list[DriveFile], name: str) -> DriveFile | None:
    f = _find_child(files, name)
    if f and _is_folder(f):
        return f
    return None


def _find_state_file(files: list[DriveFile]) -> DriveFile | None:
    return _find_child(files, "run_state.yaml")


def _read_text(client: DriveClient, file: DriveFile) -> str:
    return client.get_content(file.id, file.mime_type).content


# ---- manifest-driven skill attribution (ported from sync.py) ----
def _manifest_path_to_regex(path: str) -> re.Pattern[str]:
    escaped = re.escape(path)
    escaped = escaped.replace(r"YYYY\-MM\-DD", r"\d{4}-\d{2}-\d{2}")
    return re.compile(rf"^{escaped}$")


def _artifact_matchers(
    artifacts: list[ManifestEntry],
) -> list[tuple[re.Pattern[str], str]]:
    """Build (regex, produced_by) pairs from the manifest. ``external``
    entries (human inputs, not skill outputs) are skipped."""
    out: list[tuple[re.Pattern[str], str]] = []
    for art in artifacts:
        path = art.get("path") or ""
        producer = art.get("produced_by") or art.get("producedBy") or ""
        if not path or not producer or producer == "external":
            continue
        out.append((_manifest_path_to_regex(path), producer))
    return out


_FILENAME_PREFIX_RE = re.compile(r"^([a-z0-9][a-z0-9-]*?)(?:_|-eval[_.]|\.)")


def _filename_prefix_skill(
    f: DriveFile, registered_skills: set[str]
) -> str | None:
    """Attribute a file via its ``<skill>_…`` / ``<skill>-eval_…`` prefix when
    it lives under a phase-prefixed ``<N>-<phase>/`` folder."""
    parts = f.path.split("/")
    if len(parts) < 2 or not re.match(r"^\d+-", parts[0]):
        return None
    name = parts[-1]
    m = _FILENAME_PREFIX_RE.match(name)
    if not m:
        return None
    candidate = m.group(1)
    if candidate in registered_skills:
        return candidate
    if candidate.endswith("-eval"):
        target = candidate[: -len("-eval")]
        if target in registered_skills:
            return target
    return None


def _attribute_files_to_skills(
    files: list[DriveFile],
    matchers: list[tuple[re.Pattern[str], str]],
    registered_skills: set[str] | None = None,
) -> dict[str, list[DriveFile]]:
    """Group Drive files by producing skill: manifest match first, then the
    ``<N>-<phase>/<skill>_<role>`` filename-prefix fallback."""
    by_skill: dict[str, list[DriveFile]] = {}
    for f in files:
        if _is_folder(f):
            continue
        matched: str | None = None
        for pattern, producer in matchers:
            if pattern.match(f.path):
                matched = producer
                break
        if matched is None and registered_skills:
            matched = _filename_prefix_skill(f, registered_skills)
        key = matched or ""
        by_skill.setdefault(key, []).append(f)
    return by_skill


def _drive_file_to_artifact_ref(f: DriveFile) -> ArtifactRef:
    return ArtifactRef(
        name=f.name,
        drive_file_id=f.id,
        drive_web_link=f.web_view_link,
        size_bytes=f.size_bytes,
        mime_type=f.mime_type,
        path=f.path,
    )


# ---- verdict / QA / decisions file loaders (ported from sync.py) ----
_OLD_VERDICT_PATH_RE = re.compile(r"^verdicts/(?P<stem>[^/]+)\.ya?ml$")
_NEW_VERDICT_PATH_RE = re.compile(
    r"^[^/]+/(?P<producer>[^/]+?)_verdict(?P<variant>-[a-z]+)?\.ya?ml$"
)
_QA_RESULT_PATH_RE = re.compile(r"^[^/]+/(?P<qa_skill>[^/]+?-qa)_result\.ya?ml$")

_VARIANT_RANK = {"-deep": 4, "-monitor": 3, "-shallow": 2, "-quick": 1}


def _variant_rank(path: str) -> int:
    for suffix, score in _VARIANT_RANK.items():
        if suffix in path:
            return score
    return 0


def _load_verdicts(
    client: DriveClient,
    run_files: list[DriveFile],
    registered_skills: set[str] | None = None,
) -> dict[str, JudgeVerdict]:
    """Read every verdict YAML in the tree → ``{skill: JudgeVerdict}``.

    Matches both the old ``verdicts/<skill>[-variant].yaml`` and the new
    ``<N>-<phase>/<producer>[-eval]_verdict[-variant].yaml`` layouts; keeps the
    latest per skill (variant rank, then ``evaluated_at``)."""
    candidates: dict[str, tuple[int, str, JudgeVerdict]] = {}
    for f in run_files:
        if _is_folder(f):
            continue
        skill: str | None = None
        old = _OLD_VERDICT_PATH_RE.match(f.path)
        if old is not None:
            skill = _skill_from_verdict_stem(old.group("stem"))
        else:
            new = _NEW_VERDICT_PATH_RE.match(f.path)
            if new is not None:
                producer = new.group("producer")
                if registered_skills is not None:
                    skill = _skill_from_verdict_producer(producer, registered_skills)
                else:
                    skill = (
                        producer[: -len("-eval")]
                        if producer.endswith("-eval")
                        else producer
                    )
        if skill is None:
            continue
        try:
            body = _read_text(client, f)
        except Exception as exc:  # noqa: BLE001
            log.warning("Failed to read verdict %s: %s", f.path, exc)
            continue
        verdict = _parse_verdict_yaml(body)
        if verdict is None:
            continue
        rank = _variant_rank(f.path)
        ts = str(verdict.evaluated_at) if verdict.evaluated_at else ""
        existing = candidates.get(skill)
        if existing is None or (rank, ts) > (existing[0], existing[1]):
            candidates[skill] = (rank, ts, verdict)
    return {skill: v for skill, (_, _, v) in candidates.items()}


def _load_qa_results(
    client: DriveClient,
    run_files: list[DriveFile],
) -> dict[str, QAResult]:
    """Walk for ``<phase>/<producer>-qa_result.yaml`` → ``{target_skill:
    QAResult}``. Multiple results per skill coalesce by ``ran_at`` (latest)."""
    candidates: dict[str, tuple[str, QAResult]] = {}
    for f in run_files:
        if _is_folder(f):
            continue
        match = _QA_RESULT_PATH_RE.match(f.path)
        if match is None:
            continue
        qa_skill = match.group("qa_skill")
        try:
            body = _read_text(client, f)
        except Exception as exc:  # noqa: BLE001
            log.warning("Failed to read QA result %s: %s", f.path, exc)
            continue
        result = _parse_qa_result_yaml(body, qa_skill)
        if result is None:
            continue
        ts = result.ran_at or ""
        existing = candidates.get(result.target_skill)
        if existing is None or ts > existing[0]:
            candidates[result.target_skill] = (ts, result)
    return {skill: r for skill, (_, r) in candidates.items()}


def _load_decisions(
    client: DriveClient,
    run_files: list[DriveFile],
) -> list:
    """Read ``decisions.yaml`` (or ``.yml``) from the run-folder root."""
    file = _find_child(run_files, "decisions.yaml") or _find_child(
        run_files, "decisions.yml"
    )
    if file is None or _is_folder(file):
        return []
    try:
        body = _read_text(client, file)
    except Exception as exc:  # noqa: BLE001
        log.warning("Failed to read decisions.yaml: %s", exc)
        return []
    return parse_decisions_yaml(body)


# ---------------------------------------------------------------------------
# intermediate → read-model mapping
# ---------------------------------------------------------------------------
# parsers' canonical step status → schema StepStatus.
_CANONICAL_TO_SCHEMA_STATUS: dict[str, str] = {
    "pending": "pending",
    "running": "running",
    "complete": "complete",
    "skipped": "skipped",
    "qa-failed": "failed",
    "error": "failed",
}


def _coerce_forked_from(value: Any) -> str | None:
    """Coerce a run_state ``forked_from`` to the source run id.

    The read model declares ``forked_from: str | None``, but Drive carries
    two shapes in the wild: the bare run-id string this store writes, and a
    lineage BLOCK (``{run_id, phase, forked_at}``) that ace-web's opp_forker
    wrote for months on the reasoning that nothing read the field. A dict
    reaching pydantic raises ValidationError inside ``list_runs`` — which
    fails the whole opp's listing, not just the one forked run, and so 500s
    every uncached read of that opp.

    Anything we cannot reduce to an id degrades to ``None``: lineage is
    informational, and losing it must never cost the caller the run list.
    """
    if value is None:
        return None
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, dict):
        run_id = value.get("run_id")
        return str(run_id).strip() or None if run_id else None
    return None


def _coerce_dt(value: Any) -> dt.datetime | None:
    """Coerce a YAML scalar to a datetime. PyYAML auto-parses many ISO-8601
    timestamps to ``datetime`` already; strings (incl. trailing ``Z``) are
    parsed via ``fromisoformat``. Anything else → None."""
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return value
    if isinstance(value, dt.date):
        return dt.datetime(value.year, value.month, value.day, tzinfo=dt.timezone.utc)
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        try:
            return dt.datetime.fromisoformat(s)
        except ValueError:
            return None
    return None


def _snapshot_to_schema(
    snap: StepSnapshot,
) -> tuple[Step, list[Artifact], list[Verdict]]:
    """Map one ported ``StepSnapshot`` onto the read model: a Step, its
    Artifacts, and its (judge + QA) Verdicts — all keyed by the step's skill."""
    key = snap.step.skill_name
    step = Step(
        key=key,
        ordinal=snap.step.ordinal,
        title=snap.step.phase,
        status=_CANONICAL_TO_SCHEMA_STATUS.get(snap.step.status, "pending"),
        error=snap.step.error or "",
    )

    artifacts = [
        Artifact(
            step_key=key,
            name=a.name,
            url=a.drive_web_link or "",
            mime_type=a.mime_type or "",
            size=a.size_bytes,
            role=key,
            # The Drive adapter already holds both during attribution: the Drive
            # file id is the opaque stable handle; ``path`` is run-relative.
            ref=a.drive_file_id or "",
            path=a.path or "",
        )
        for a in snap.artifacts
    ]

    verdicts: list[Verdict] = []
    if snap.judge is not None:
        j = snap.judge
        verdicts.append(
            Verdict(
                step_key=key,
                kind="judge",
                score=j.score,
                passed=j.passed,
                criteria=j.criteria or {},
                rationale=j.rationale or "",
                evaluated_at=_coerce_dt(j.evaluated_at),
            )
        )
    if snap.qa_result is not None:
        q = snap.qa_result
        passed = (
            True if q.verdict == "pass" else False if q.verdict == "fail" else None
        )
        verdicts.append(
            Verdict(
                step_key=key,
                kind="qa",
                passed=passed,
                criteria={
                    "checks_run": q.checks_run,
                    "checks_passed": q.checks_passed,
                    "checks_failed": q.checks_failed,
                    "failures": [fl.check for fl in q.failures],
                },
                rationale="; ".join(fl.detail for fl in q.failures),
                evaluated_at=_coerce_dt(q.ran_at),
            )
        )
    return step, artifacts, verdicts


def _decision_to_schema(d) -> Decision:
    """Map a ported parsers.Decision row onto the read-model Decision.

    step_key is the row's ``skill`` (the producing step); falls back to the
    row ``phase`` then the row ``id`` so a row always lands on *some* key."""
    step_key = d.skill or d.phase or d.id
    return Decision(
        step_key=step_key,
        question=d.question,
        ai_default=d.ai_default,
        override=d.override,
        status=d.status if d.status in ("ai-default", "overridden") else "ai-default",
        reasoning=d.notes or d.override_reasoning or "",
        evidence_basis=d.evidence_basis or "",
        # The parsers.Decision dataclass already carries these (ported from ACE's
        # decisions-schema); thread them through instead of dropping them.
        id=d.id or "",
        phase=d.phase or "",
        options_considered=list(d.options_considered or []),
        source=d.source or "",
        override_reasoning=d.override_reasoning or "",
        conflict_signals=list(d.conflict_signals or []),
    )


def _parse_gates(state_data: dict) -> list[Gate]:
    """Read the ``gates:`` map out of run_state.yaml.

    Shape (per the ACE plugin)::

        gates:
          idea-to-pdd:
            decision: approved
            decided_by: ace@dimagi-ai.com
            decided_at: 2026-05-02T18:35:30Z
            note: ''
    """
    gates_raw = state_data.get("gates")
    if not isinstance(gates_raw, dict):
        return []
    out: list[Gate] = []
    for step_key, g in gates_raw.items():
        if not isinstance(step_key, str):
            continue
        if not isinstance(g, dict):
            continue
        out.append(
            Gate(
                step_key=step_key,
                decision=str(g.get("decision") or ""),
                decided_by=str(g.get("decided_by") or ""),
                decided_at=_coerce_dt(g.get("decided_at")),
                note=str(g.get("note") or ""),
            )
        )
    return out


# ---------------------------------------------------------------------------
# Fork helpers (ported from ace-web apps/opps/opp_forker.py)
# ---------------------------------------------------------------------------
def _mint_run_id(now: dt.datetime, existing_run_names: set[str]) -> str:
    """Build a ``YYYYMMDD-HHMM`` run-id, bumping a ``-N`` suffix on collision.

    Run-ids follow ``YYYYMMDD-HHMM`` so a lexical sort matches chronological
    order; two forks in the same minute get ``-2`` / ``-3`` (still sortable).
    Mirrors ACE's ``opp_forker._mint_run_id``."""
    base = now.strftime("%Y%m%d-%H%M")
    if base not in existing_run_names:
        return base
    suffix = 2
    while f"{base}-{suffix}" in existing_run_names:
        suffix += 1
    return f"{base}-{suffix}"


def _rewrite_fork_decisions(
    body: str,
    *,
    kept_step_keys: set[str],
    mode: str,
    edits: dict,
) -> str | None:
    """Trim + mode-filter + edit a source ``decisions.yaml`` body for a fork.

    Returns the new YAML body, or ``None`` when the source had no parseable
    decisions list (caller writes nothing). Matches the
    ``InMemoryRunStore.fork`` / ``DbRunStore.fork`` decision contract exactly:

      1. keep only rows whose ``skill`` (step key) is a kept step;
      2. drop non-overridden rows when ``mode == "keep-overrides-only"`` (the
         filter reads the row's ORIGINAL status, before any edit — so an edit
         cannot rescue a trimmed row, same as the other adapters);
      3. apply ``edits[skill][question]`` to the surviving row via the shared
         ``_apply_decision_edit`` (str → override, or a dict of
         override/status/reasoning/evidence_basis).
    """
    try:
        data = yaml.safe_load(body) or {}
    except yaml.YAMLError:
        return None
    if not isinstance(data, dict):
        return None
    rows = data.get("decisions")
    if not isinstance(rows, list):
        return None

    new_rows: list = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        skill = str(row.get("skill") or "")
        if skill not in kept_step_keys:
            continue
        original_status = str(row.get("status") or "ai-default")
        if mode == "keep-overrides-only" and original_status != "overridden":
            continue
        edit = edits.get(skill, {}).get(str(row.get("question") or ""))
        # `_apply_decision_edit` writes override/status/reasoning/evidence_basis
        # — all valid decisions.yaml v3 keys — leaving the hyphenated
        # `ai-default` untouched, so it round-trips through the parser cleanly.
        _apply_decision_edit(row, edit)
        new_rows.append(row)

    data["decisions"] = new_rows
    return yaml.safe_dump(data, sort_keys=False)


def _build_fork_run_state(
    *,
    mode: Any,
    current_step: str,
    forked_from: str,
    phases_order: list[str],
    steps_by_phase: dict[str, list[SkillMeta]],
    kept_step_keys: set[str],
    now: dt.datetime,
) -> str:
    """Synthesize a fresh ``run_state.yaml`` body for a forked run.

    Per phase (in registry order) a canonical block is emitted:
    ``{status, [verdict, completed_at], steps: {<skill>: {status}}}``. A step is
    ``done`` when kept, else ``pending``; a phase is ``done`` (with
    ``verdict: seeded`` + ``completed_at``) only when every one of its steps is
    kept, else ``pending``. ``_extract_step_statuses`` reads the ``steps`` map,
    so ``get_run`` derives kept→complete / fork-onward→pending from this.

    ``forked_from`` is written as a top-level STRING (the source run id) so the
    read model's ``Run.forked_from: str | None`` validates — matching the DB
    adapter, which stores the source pk."""
    iso_now = now.isoformat()
    run_mode = mode if mode in ("review", "auto") else "review"
    phases_map: dict[str, dict] = {}
    for phase in phases_order:
        steps = steps_by_phase[phase]
        all_kept = all(s.name in kept_step_keys for s in steps)
        block: dict = {"status": "done" if all_kept else "pending"}
        if all_kept:
            block["verdict"] = "seeded"
            block["completed_at"] = iso_now
        block["steps"] = {
            s.name: {"status": "done" if s.name in kept_step_keys else "pending"}
            for s in steps
        }
        phases_map[phase] = block

    data: dict = {
        "mode": run_mode,
        "started_at": iso_now,
        "current_step": current_step,
        "forked_from": forked_from,
        "phases": phases_map,
    }
    return yaml.safe_dump(data, sort_keys=False)


# ---------------------------------------------------------------------------
# DriveRunStore
# ---------------------------------------------------------------------------
class DriveRunStore:
    """A `RunStore` reading ACE-shaped Drive run-folders into the read model.

    Bound to ONE agent's Drive root folder (the folder that contains
    ``runs/<run-id>/`` and, optionally, ``opp.yaml``). The ``agent`` slug
    passed to each method stamps ``Run.agent_slug`` / ``RunSummary.agent_slug``
    and is cross-checked against ``agent_slug`` when that was pinned at
    construction.

    Layout read (mirrors ACE multi-run)::

        <root>/opp.yaml                       (optional opp-level metadata)
        <root>/runs/<run-id>/run_state.yaml   (phases map, mode, gates)
        <root>/runs/<run-id>/decisions.yaml   (decisions log)
        <root>/runs/<run-id>/verdicts/*.yaml  (old-layout judge verdicts)
        <root>/runs/<run-id>/<N>-<phase>/*    (artifacts + new-layout verdicts/QA)
    """

    def __init__(
        self,
        client: DriveClient,
        root_folder_id: str,
        *,
        agent_slug: str | None = None,
        manifest: list[ManifestEntry] | None = None,
        skill_registry: list[SkillMeta] | None = None,
    ) -> None:
        self.client = client
        self.root_folder_id = root_folder_id
        self.agent_slug = agent_slug
        self.manifest = list(manifest if manifest is not None else DEFAULT_MANIFEST)
        self.skill_registry = list(
            skill_registry if skill_registry is not None else DEFAULT_SKILL_REGISTRY
        )

    # -- internal resolution --
    def _registered_skills(self) -> set[str]:
        return {s.name for s in self.skill_registry}

    def _runs_folder_id(self) -> str | None:
        children = self.client.list_folder(self.root_folder_id)
        runs = _find_child_folder(children, "runs")
        return runs.id if runs is not None else None

    def _run_folder(self, run_id: str) -> DriveFile:
        runs_id = self._runs_folder_id()
        if runs_id is None:
            raise KeyError(f"no runs/ folder under root {self.root_folder_id!r}")
        for child in self.client.list_folder(runs_id):
            if _is_folder(child) and child.name == run_id:
                return child
        raise KeyError(f"no run {run_id!r} under runs/")

    def _read_state(self, run_children: list[DriveFile]) -> tuple[dict, DriveFile | None]:
        state_file = _find_state_file(run_children)
        if state_file is None:
            return {}, None
        try:
            data = yaml.safe_load(_read_text(self.client, state_file)) or {}
        except yaml.YAMLError:
            log.warning("run_state.yaml is not valid YAML")
            data = {}
        if not isinstance(data, dict):
            data = {}
        return data, state_file

    def _opp_display_name(self) -> str | None:
        children = self.client.list_folder(self.root_folder_id)
        opp_yaml = _find_child(children, "opp.yaml")
        if opp_yaml is None:
            return None
        try:
            data = yaml.safe_load(_read_text(self.client, opp_yaml)) or {}
        except yaml.YAMLError:
            return None
        if isinstance(data, dict):
            return data.get("display_name")
        return None

    def _build_snapshots(
        self, run_folder_id: str, state_data: dict
    ) -> list[StepSnapshot]:
        """The ported ``_load_opp_run`` core: attribute files, parse verdicts/
        QA/decisions, synthesize steps with run_state as the status source."""
        run_tree = self.client.list_files(run_folder_id, recursive=True)
        registered = self._registered_skills()

        matchers = _artifact_matchers(self.manifest)
        files_by_skill = _attribute_files_to_skills(run_tree, matchers, registered)
        artifacts_by_skill: dict[str, list[ArtifactRef]] = {
            skill: [_drive_file_to_artifact_ref(f) for f in files]
            for skill, files in files_by_skill.items()
            if skill
        }

        verdicts_by_skill = _load_verdicts(self.client, run_tree, registered)
        qa_results_by_skill = _load_qa_results(self.client, run_tree)

        return _build_steps(
            self.skill_registry,
            artifacts_by_skill,
            verdicts_by_skill,
            run_folder_id,
            qa_results_by_skill=qa_results_by_skill,
            step_status_by_skill=_extract_step_statuses(state_data),
        )

    def _run_header_fields(self, agent: str, run_id: str, state_data: dict) -> dict:
        # ACE's run_state.yaml writes the autopilot mode as the literal
        # "autopilot" (see the malaria-rdt-simple fixture). The canopy read
        # model's RunMode enum canonicalizes that to "auto", so map it here.
        # Previously any non-"review"/"auto" value (including ACE's own
        # "autopilot") silently collapsed to "review" — a real parity bug that
        # dropped the autopilot signal on every ACE run.
        mode = state_data.get("mode")
        if mode == "autopilot":
            mode = "auto"
        if mode not in ("review", "auto"):
            mode = "review"
        label = (
            self._opp_display_name()
            or state_data.get("display_name")
            or agent
        )
        return {
            "id": run_id,
            "agent_slug": agent,
            "label": label,
            "mode": mode,
            # current_phase is read straight from run_state.yaml (matching ACE's
            # RunDetail.current_phase) — the read model previously omitted it.
            "current_phase": str(state_data.get("current_phase") or ""),
            "current_step": str(
                state_data.get("current_step") or state_data.get("step") or ""
            ),
            "forked_from": _coerce_forked_from(state_data.get("forked_from")),
            "session_link": str(state_data.get("session_link") or ""),
            "created_at": _coerce_dt(
                state_data.get("started_at") or state_data.get("created")
            ),
            "completed_at": _coerce_dt(state_data.get("completed_at")),
        }

    # -- RunStore: reads --
    def get_run(self, agent: str, run_id: str) -> Run:
        run_folder = self._run_folder(run_id)
        run_children = self.client.list_folder(run_folder.id)
        state_data, _ = self._read_state(run_children)

        snapshots = self._build_snapshots(run_folder.id, state_data)
        steps: list[Step] = []
        artifacts: list[Artifact] = []
        verdicts: list[Verdict] = []
        for snap in snapshots:
            step, arts, verds = _snapshot_to_schema(snap)
            steps.append(step)
            artifacts.extend(arts)
            verdicts.extend(verds)

        run_tree = self.client.list_files(run_folder.id, recursive=True)
        decisions = [_decision_to_schema(d) for d in _load_decisions(self.client, run_tree)]
        gates = _parse_gates(state_data)

        run = Run(
            steps=steps,
            artifacts=artifacts,
            verdicts=verdicts,
            decisions=decisions,
            gates=gates,
            **self._run_header_fields(agent, run_id, state_data),
        )
        return run.with_derived_status()

    def _state_files_by_parent(self, run_folders: list[DriveFile]) -> dict[str, DriveFile]:
        """{run_folder_id: run_state.yaml DriveFile} for every run that has one.

        Uses the client's `find_in_folders` when offered — one query instead of
        one list_folder per run. Falls back to the per-folder listing so a
        client without it behaves exactly as before.
        """
        finder = getattr(self.client, "find_in_folders", None)
        if callable(finder):
            found = finder([f.id for f in run_folders], "run_state.yaml")
            return {parent: file for parent, file in found.items() if file is not None}
        out: dict[str, DriveFile] = {}
        for child in run_folders:
            state_file = _find_state_file(self.client.list_folder(child.id))
            if state_file is not None:
                out[child.id] = state_file
        return out

    def _read_states_bulk(self, files: list[DriveFile]) -> dict[str, dict]:
        """{file_id: parsed run_state} — concurrently when the client can."""
        bulk = getattr(self.client, "get_contents", None)
        if callable(bulk) and files:
            raw = bulk([(f.id, f.mime_type) for f in files])
        else:
            raw = {}
            for f in files:
                try:
                    raw[f.id] = _read_text(self.client, f)
                except Exception:  # noqa: BLE001 - one bad file must not sink the list
                    log.warning("could not read run_state for %s", f.id)
        out: dict[str, dict] = {}
        for file_id, text in raw.items():
            if text is None:
                continue
            try:
                data = yaml.safe_load(text) or {}
            except yaml.YAMLError:
                log.warning("run_state.yaml is not valid YAML (%s)", file_id)
                data = {}
            out[file_id] = data if isinstance(data, dict) else {}
        return out

    def list_runs(self, agent: str) -> list[RunSummary]:
        runs_id = self._runs_folder_id()
        if runs_id is None:
            return []
        run_folders = [c for c in self.client.list_folder(runs_id) if _is_folder(c)]
        if not run_folders:
            return []

        # ── Why this is not a plain loop ────────────────────────────────────
        # The obvious shape — for each run: list its folder, then download
        # run_state.yaml — costs 1 + 2N Drive round-trips, SEQUENTIALLY. On a
        # real opp (12 runs) that measured ~25 calls and 30-50 s of wall clock,
        # behind a 30 s content cache that a 50 s load can never populate. So
        # every page view paid full price.
        #
        # Two optional fast paths, both negotiated off the client so any
        # implementation that lacks them still works:
        #   find_in_folders  — one query for run_state.yaml across ALL run
        #                      folders, replacing N list_folder calls.
        #   get_contents     — the N downloads concurrently. Kept on the client
        #                      because thread-safety is the client's business:
        #                      googleapiclient's service object is not
        #                      thread-safe and only the implementation knows
        #                      how to hand each worker its own transport.
        state_by_parent = self._state_files_by_parent(run_folders)
        contents = self._read_states_bulk(list(state_by_parent.values()))

        summaries: list[RunSummary] = []
        for child in run_folders:
            state_file = state_by_parent.get(child.id)
            if state_file is None:
                continue  # half-initialized run folder
            state_data = contents.get(state_file.id, {})
            # Cheap status: synthesize steps from run_state alone (no tree walk).
            snaps = _build_steps(
                self.skill_registry,
                {},
                {},
                child.id,
                step_status_by_skill=_extract_step_statuses(state_data),
            )
            steps = [_snapshot_to_schema(s)[0] for s in snaps]
            hdr = self._run_header_fields(agent, child.name, state_data)
            summaries.append(
                RunSummary(
                    id=hdr["id"],
                    agent_slug=hdr["agent_slug"],
                    label=hdr["label"],
                    mode=hdr["mode"],
                    status=derive_status(steps),
                    current_phase=hdr["current_phase"],
                    current_step=hdr["current_step"],
                    forked_from=hdr["forked_from"],
                    session_link=hdr["session_link"],
                    created_at=hdr["created_at"],
                    completed_at=hdr["completed_at"],
                )
            )
        summaries.sort(
            key=lambda s: (
                s.created_at or dt.datetime.min.replace(tzinfo=dt.timezone.utc),
                s.id,
            ),
            reverse=True,
        )
        return summaries

    def list_steps(self, agent: str, run_id: str) -> list[Step]:
        return self.get_run(agent, run_id).steps

    def list_artifacts(
        self, agent: str, run_id: str, step_key: str | None = None
    ) -> list[Artifact]:
        arts = self.get_run(agent, run_id).artifacts
        if step_key is not None:
            return [a for a in arts if a.step_key == step_key]
        return arts

    def list_verdicts(self, agent: str, run_id: str) -> list[Verdict]:
        return self.get_run(agent, run_id).verdicts

    # -- RunStore: writes --
    def record_verdict(
        self, agent: str, run_id: str, step_key: str, *,
        kind: str, score: float | None = None, passed: bool | None = None,
        criteria: dict | None = None, rationale: str = "",
        evaluated_at: dt.datetime | None = None,
    ) -> Verdict:
        """Not supported: the Drive adapter is read-through for verdicts. ACE
        writes its own ``verdicts/*.yaml`` (and new-layout per-phase verdicts);
        `list_verdicts` reads them. Canopy-hosted agents record verdicts through
        the DB store (`DbRunStore.record_verdict`)."""
        raise NotImplementedError(
            "DriveRunStore is read-through for verdicts: ACE writes verdicts to "
            "Drive directly; record them via the DB store for canopy-hosted agents."
        )

    def record_gate(
        self,
        agent: str,
        run_id: str,
        step_key: str,
        decision: str,
        decided_by: str = "",
        note: str = "",
    ) -> Gate:
        """Record (close) a gate by read-modify-writing run_state.yaml's
        ``gates:`` map via the DriveClient (``update_file``)."""
        run_folder = self._run_folder(run_id)
        run_children = self.client.list_folder(run_folder.id)
        state_data, state_file = self._read_state(run_children)
        if state_file is None:
            raise KeyError(f"run {run_id!r} has no run_state.yaml to record a gate")

        now = dt.datetime.now(dt.timezone.utc)
        decided_at_iso = now.isoformat()
        gates = state_data.get("gates")
        if not isinstance(gates, dict):
            gates = {}
        gates[step_key] = {
            "decision": decision,
            "decided_by": decided_by,
            "decided_at": decided_at_iso,
            "note": note,
        }
        state_data["gates"] = gates

        self.client.update_file(
            state_file.id,
            yaml.safe_dump(state_data, sort_keys=False, default_flow_style=False),
            "application/x-yaml",
        )
        return Gate(
            step_key=step_key,
            decision=decision,
            decided_by=decided_by,
            decided_at=now,
            note=note,
        )

    def record_decision(
        self, agent: str, run_id: str, step_key: str, decision_fields: dict
    ) -> Decision:
        """Append a row to the run's ``decisions.yaml`` (read-modify-write)."""
        run_folder = self._run_folder(run_id)
        run_children = self.client.list_folder(run_folder.id)

        existing_file = _find_child(run_children, "decisions.yaml") or _find_child(
            run_children, "decisions.yml"
        )
        rows: list[dict] = []
        if existing_file is not None and not _is_folder(existing_file):
            try:
                data = yaml.safe_load(_read_text(self.client, existing_file)) or {}
            except yaml.YAMLError:
                data = {}
            if isinstance(data, dict) and isinstance(data.get("decisions"), list):
                rows = list(data["decisions"])

        q = str(decision_fields.get("question") or "")
        new_row = {
            "id": decision_fields.get("id") or f"{step_key}-{len(rows) + 1}",
            "phase": decision_fields.get("phase") or "",
            "skill": step_key,
            "question": q,
            "ai-default": decision_fields.get("ai_default") or "",
            "override": decision_fields.get("override") or "",
            "status": decision_fields.get("status") or "ai-default",
            "reasoning": decision_fields.get("reasoning") or "",
            "evidence_basis": decision_fields.get("evidence_basis") or "stated",
        }
        rows.append(new_row)
        body = yaml.safe_dump({"decisions": rows}, sort_keys=False)

        if existing_file is not None and not _is_folder(existing_file):
            self.client.update_file(existing_file.id, body, "application/x-yaml")
        else:
            # No decisions.yaml yet — create via the fake/real upload helper.
            upload = getattr(self.client, "upload_file", None)
            if upload is None:
                raise NotImplementedError(
                    "DriveClient has no upload_file; cannot create decisions.yaml"
                )
            upload(run_folder.id, "decisions.yaml", body, "application/x-yaml")

        return Decision(
            step_key=step_key,
            question=q,
            ai_default=str(decision_fields.get("ai_default") or ""),
            override=str(decision_fields.get("override") or ""),
            status=(
                decision_fields.get("status")
                if decision_fields.get("status") in ("ai-default", "overridden")
                else "ai-default"
            ),
            reasoning=str(decision_fields.get("reasoning") or ""),
            evidence_basis=str(decision_fields.get("evidence_basis") or ""),
        )

    def fork(
        self,
        agent: str,
        run_id: str,
        at_step: str,
        mode: str = "keep-overrides-only",
        edits: dict | None = None,
    ) -> RunSummary:
        """Mint a new run-id folder under the SAME agent root, seeded from
        ``run_id``'s outputs up to (but not including) ``at_step``.

        Ports ACE's ``opp_forker.fork_opp`` to the canopy ``RunStore`` contract
        (step-key fork point + the ``FORK_MODES`` shared with the DB/in-memory
        adapters) so the three are interchangeable through the read model:

        * Phase folders ``<phase>/`` whose every step is kept (ordinal <
          ``at_step``'s ordinal) are copied verbatim — the plugin lays artifacts
          out in per-phase folders, so a wholly-kept phase carries its artifacts.
        * ``idea.md`` / ``inputs-manifest.yaml`` carry over verbatim.
        * ``decisions.yaml`` is rewritten: only kept-step rows survive, the
          ``mode`` filter drops upstream AI-defaults (keep-overrides-only) or
          keeps everything (keep-all), then ``edits`` are applied — matching
          ``InMemoryRunStore.fork`` / ``DbRunStore.fork`` exactly.
        * A fresh ``run_state.yaml`` is synthesized: kept steps ``done`` (their
          phase carries ``verdict: seeded`` + ``completed_at``), ``at_step``
          onward ``pending``. NOT copied from the source — its phases/timestamps
          belong to the prior run.

        Returns a ``RunSummary`` for the new run (built from a fresh
        ``get_run`` so the derived status / header are read-model-consistent).
        """
        if mode not in FORK_MODES:
            raise ValueError(
                f"unknown fork mode {mode!r}; expected one of {FORK_MODES}"
            )
        edits = edits or {}

        fork_meta = next(
            (s for s in self.skill_registry if s.name == at_step), None
        )
        if fork_meta is None:
            raise ValueError(f"no step {at_step!r} in the skill registry")
        fork_ordinal = fork_meta.ordinal

        # Step-ordinal trim (matches the DB/in-memory adapters): a step/decision
        # is "kept" when its ordinal is strictly before the fork point. A phase
        # folder is copied only when ALL its steps are kept (phase-granular Drive
        # artifacts; step-granular statuses) — at a phase boundary the two agree.
        kept_step_keys = {
            s.name for s in self.skill_registry if s.ordinal < fork_ordinal
        }
        phases_order: list[str] = []
        steps_by_phase: dict[str, list[SkillMeta]] = {}
        for s in self.skill_registry:
            if s.phase not in steps_by_phase:
                steps_by_phase[s.phase] = []
                phases_order.append(s.phase)
            steps_by_phase[s.phase].append(s)
        phases_fully_kept = {
            phase
            for phase, steps in steps_by_phase.items()
            if steps and all(s.name in kept_step_keys for s in steps)
        }

        # Resolve the source run + the runs/ folder it lives under.
        source_run = self._run_folder(run_id)
        runs_id = self._runs_folder_id()
        if runs_id is None:
            raise KeyError(f"no runs/ folder under root {self.root_folder_id!r}")
        run_children = self.client.list_folder(source_run.id)
        source_state, _ = self._read_state(run_children)

        upload = getattr(self.client, "upload_file", None)
        if upload is None:
            raise NotImplementedError(
                "DriveClient has no upload_file; cannot synthesize the fork's "
                "run_state.yaml"
            )

        now = dt.datetime.now(dt.timezone.utc)
        existing_run_names = {
            c.name for c in self.client.list_folder(runs_id) if _is_folder(c)
        }
        new_run_id = _mint_run_id(now, existing_run_names)
        new_run_folder_id = self.client.create_folder(runs_id, new_run_id)

        # Copy kept phase subtrees + carried run-root files verbatim.
        for child in run_children:
            if _is_folder(child):
                if child.name in phases_fully_kept:
                    sub_id = self.client.create_folder(new_run_folder_id, child.name)
                    self._copy_folder_verbatim(child.id, sub_id)
            elif child.name in _FORK_RUN_ROOT_FILES:
                self.client.copy_file(child.id, new_run_folder_id, child.name)

        # Carry (rewrite) decisions.yaml: trim to kept steps, mode-filter, edit.
        decisions_file = _find_child(run_children, "decisions.yaml") or _find_child(
            run_children, "decisions.yml"
        )
        if decisions_file is not None and not _is_folder(decisions_file):
            try:
                src_body = _read_text(self.client, decisions_file)
            except Exception as exc:  # noqa: BLE001
                log.warning("Failed to read source decisions.yaml: %s", exc)
                src_body = ""
            new_body = _rewrite_fork_decisions(
                src_body, kept_step_keys=kept_step_keys, mode=mode, edits=edits
            )
            if new_body is not None:
                upload(new_run_folder_id, decisions_file.name, new_body, "application/x-yaml")

        # Synthesize a fresh run_state.yaml (kept→done/seeded, fork→pending).
        new_state = _build_fork_run_state(
            mode=source_state.get("mode"),
            current_step=at_step,
            forked_from=run_id,
            phases_order=phases_order,
            steps_by_phase=steps_by_phase,
            kept_step_keys=kept_step_keys,
            now=now,
        )
        upload(new_run_folder_id, "run_state.yaml", new_state, "application/x-yaml")

        new_run = self.get_run(agent, new_run_id)
        return RunSummary(
            id=new_run.id,
            agent_slug=new_run.agent_slug,
            label=new_run.label,
            mode=new_run.mode,
            status=new_run.status_from_steps(),
            current_phase=new_run.current_phase,
            current_step=new_run.current_step,
            forked_from=new_run.forked_from,
            session_link=new_run.session_link,
            created_at=new_run.created_at,
            completed_at=new_run.completed_at,
        )

    def _copy_folder_verbatim(self, source_folder_id: str, dest_folder_id: str) -> None:
        """Recursively copy every child of ``source_folder_id`` into
        ``dest_folder_id`` (folders recreated, files copied). No filtering — the
        caller decides which top-level phase folders to copy."""
        for child in self.client.list_folder(source_folder_id):
            if _is_folder(child):
                sub_id = self.client.create_folder(dest_folder_id, child.name)
                self._copy_folder_verbatim(child.id, sub_id)
            else:
                self.client.copy_file(child.id, dest_folder_id, child.name)

    def create_run(
        self,
        agent: str,
        *,
        label: str = "",
        mode: str = "review",
        current_step: str = "",
        session_link: str = "",
        steps: list[dict] | None = None,
    ) -> RunSummary:
        """Drive runs are minted by ACE itself (it writes ``run_state.yaml`` into
        the opp's Drive tree), not created through canopy's REST surface. This is
        the deliberate seam: the unified-run API can only *create* DB-as-truth
        runs. If ACE ever writes back through canopy this is where it lands."""
        raise NotImplementedError(
            "DriveRunStore is read/gate/fork-only; Drive runs are created by ACE, "
            "not via the canopy run API."
        )

    # -- RunStore: cache invalidation --
    def changed_ids(
        self, agent: str, cursor: str | None = None
    ) -> tuple[list[str], str]:
        """Run ids whose files changed since ``cursor``, via the Drive Changes
        API. First call (no cursor) seeds a token and reports nothing changed —
        the standard Drive pattern (you can't enumerate "all changes since the
        beginning of time" cheaply)."""
        if cursor is None:
            token = self.client.get_changes_start_page_token()
            return [], token

        page = self.client.list_changes(cursor)
        if page.expired:
            token = self.client.get_changes_start_page_token()
            return [], token
        if not page.changed_file_ids:
            return [], page.next_page_token

        # Map changed file ids → owning run id by walking each run's tree.
        file_to_run = self._file_id_to_run_id()
        changed_runs: list[str] = []
        seen: set[str] = set()
        for fid in page.changed_file_ids:
            run_id = file_to_run.get(fid)
            if run_id and run_id not in seen:
                seen.add(run_id)
                changed_runs.append(run_id)
        return changed_runs, page.next_page_token

    def _file_id_to_run_id(self) -> dict[str, str]:
        """Map every file/folder id under each run folder to that run id."""
        runs_id = self._runs_folder_id()
        out: dict[str, str] = {}
        if runs_id is None:
            return out
        for child in self.client.list_folder(runs_id):
            if not _is_folder(child):
                continue
            out[child.id] = child.name  # the run folder itself
            for f in self.client.list_files(child.id, recursive=True):
                out[f.id] = child.name
        return out
