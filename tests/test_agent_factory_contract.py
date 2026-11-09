"""The contract canopy-web relies on from `canopy-agent-factory`.

WHY THIS FILE EXISTS. The factory lives in another repo (dimagi-internal/canopy)
and is consumed here from PyPI at `>=1,<2`. Both repos commit heavily — canopy
is past 0.2.4xx — so a minor release flows in on the next lock refresh with
nobody reading its diff. This pins the surface canopy-web actually calls, so a
breaking change fails HERE, in CI, rather than the first time somebody tries to
create an agent from the browser.

It is deliberately a CONTRACT test, not a behaviour test: it asserts the shape
canopy-web depends on (names, signatures, return types) and leaves what the
scaffold *contains* to the factory's own suite, which is where that belongs.
Duplicating the factory's tests here would be a second copy free to drift.

Same reasoning as tests/test_claim_schedule_parity.py, which exists so a
divergence between two authorization paths "fails CI instead of production".
"""
from __future__ import annotations

import inspect
from pathlib import Path

import pytest

factory = pytest.importorskip(
    "canopy_agent_factory",
    reason="canopy-agent-factory is a hard dependency; a skip here means the "
    "environment is broken, not that the test is optional",
)


def test_the_public_surface_exists():
    """The four names canopy-web imports, plus the two accessors."""
    for name in (
        "AgentSpec",
        "create_agent",
        "normalize_slug",
        "AgentFactoryError",
        "templates",
        "gating_config",
    ):
        assert hasattr(factory, name), f"canopy-agent-factory no longer exports {name}"


def test_create_agent_still_takes_a_caller_supplied_directory():
    """The property that makes a second consumer possible at all.

    `create_agent(spec, target_dir, *, force=False) -> list[Path]` renders into a
    directory the CALLER chooses and returns what it wrote. If the factory ever
    reverts to owning the path (or to running `git init` itself), canopy-web
    cannot use it from a web request — it needs to scaffold into a temp dir and
    push the result over the GitHub API.
    """
    sig = inspect.signature(factory.create_agent)
    params = list(sig.parameters)
    assert params[0] == "spec", f"first parameter is {params[0]!r}, expected 'spec'"
    assert params[1] == "target_dir", (
        f"second parameter is {params[1]!r}, expected 'target_dir' — the factory "
        "must render where the caller says, not where it chooses"
    )
    assert sig.parameters["force"].kind is inspect.Parameter.KEYWORD_ONLY


def test_agent_spec_accepts_the_fields_canopy_web_supplies():
    """The fields a browser form can realistically collect."""
    spec = factory.AgentSpec(
        slug="contract-probe",
        display_name="Contract Probe",
        mandate="Prove the constructor still accepts what canopy-web sends.",
    )
    assert spec.slug == "contract-probe"


def test_normalize_slug_rejects_what_it_should_and_raises_the_declared_error():
    """canopy-web catches `AgentFactoryError` to turn a bad slug into a 422.

    If the factory started raising something else, that handler would stop
    catching and a bad slug would become a 500.
    """
    assert factory.normalize_slug("Contract Probe") == "contract-probe"
    with pytest.raises(factory.AgentFactoryError):
        factory.normalize_slug("")


def test_templates_is_a_non_empty_read_only_mapping():
    """The fleet-alignment baseline. Read-only so a consumer cannot corrupt it."""
    t = factory.templates()
    assert len(t) > 0, "the stamp table is empty — the factory has lost its templates"
    with pytest.raises(TypeError):
        t["injected"] = "should not be possible"  # type: ignore[index]


def test_scaffolding_into_a_temp_dir_writes_files_and_returns_their_paths(tmp_path: Path):
    """One end-to-end call, because the signature can be right while the call fails.

    Deliberately asserts only that it wrote something and told us where — WHAT it
    wrote is the factory's own suite's business.
    """
    spec = factory.AgentSpec(
        slug="contract-probe",
        display_name="Contract Probe",
        mandate="End-to-end contract probe.",
    )
    written = factory.create_agent(spec, tmp_path / "contract-probe")
    assert written, "create_agent returned no paths"
    assert all(isinstance(p, Path) for p in written), "expected a list of Path"
    assert all(p.exists() for p in written), "create_agent reported paths it did not write"


def test_it_has_no_runtime_dependencies():
    """The reason the factory was split out of `canopy` at all.

    canopy itself depends on Pillow and NumPy for the DDD screenshot gates.
    Pulling those into a Django web image to render text templates is what the
    split avoided — and if this package grows a dependency, that reason quietly
    stops holding.
    """
    from importlib.metadata import requires

    assert not (requires("canopy-agent-factory") or []), (
        "canopy-agent-factory has grown runtime dependencies; the whole point of "
        "extracting it was that it had none"
    )


def test_a_scaffolded_agent_carries_the_current_state_guard_and_its_workflow(tmp_path: Path):
    """A new agent is born with the fleet's docs guard wired into CI, and the guard passes.

    The factory stamps `tests/test_docs_current_state.py`, a `.github/workflows/test.yml`
    that runs it, and a CLAUDE.md Conventions line. The scaffold is rendered to disk and the
    stamped guard is run with pytest against the fresh repo, so a factory release whose
    guard fails on its own scaffold fails here before a browser-created agent hits it.
    """
    import subprocess
    import sys

    spec = factory.AgentSpec(
        slug="guard-probe",
        display_name="Guard Probe",
        mandate="Prove the stamped guard passes on a fresh scaffold.",
        mailbox="guard-probe@example.org",
    )
    root = tmp_path / "guard-probe"
    written = {p.relative_to(root).as_posix() for p in factory.create_agent(spec, root)}
    assert "tests/test_docs_current_state.py" in written
    assert ".github/workflows/test.yml" in written
    assert "test_docs_current_state.py" in (root / ".github/workflows/test.yml").read_text()
    assert "test_docs_current_state" in (root / "CLAUDE.md").read_text()

    done = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests"],
        cwd=root, capture_output=True, text=True, timeout=120,
    )
    assert done.returncode == 0, done.stdout + done.stderr
