"""The cap checker's RULE, without a network.

The script asks PyPI; these hand it a fake answer, so what is being tested is
the decision — frozen, held, stale — and not whether PyPI was up.
"""

import importlib.util
import sys
from pathlib import Path

import pytest
from packaging.version import Version

_SPEC = importlib.util.spec_from_file_location(
    "check_dependency_caps",
    Path(__file__).resolve().parents[1] / "scripts" / "check_dependency_caps.py",
)
caps = importlib.util.module_from_spec(_SPEC)
# Registered before executing: `@dataclass` looks its own module up in
# sys.modules, and `scripts/` is deliberately not a package.
sys.modules[_SPEC.name] = caps
_SPEC.loader.exec_module(caps)


def _pyproject(*lines: str) -> str:
    body = "\n".join(f"    {line}" for line in lines)
    return f'[project]\nname = "x"\ndependencies = [\n{body}\n]\n'


def _latest(**versions: str):
    return lambda name: Version(versions[name]) if name in versions else None


def test_a_cap_that_excludes_the_latest_release_is_a_freeze():
    """The allauth / anthropic / ag-ui shape."""
    findings, checked, _ = caps.find_frozen(
        _pyproject('"ag-ui-protocol>=0.1.22,<0.2",'), _latest(**{"ag-ui-protocol": "1.0.0"})
    )

    assert [(f.kind, f.name, f.latest) for f in findings] == [("frozen", "ag-ui-protocol", "1.0.0")]
    assert checked == 1


def test_a_cap_that_admits_the_latest_release_is_fine():
    findings, _, _ = caps.find_frozen(_pyproject('"django>=6.1,<7.0",'), _latest(django="6.1.1"))

    assert findings == []


def test_an_uncapped_requirement_is_not_asked_about():
    asked = []

    caps.find_frozen(_pyproject('"requests>=2",'), lambda n: asked.append(n))

    assert asked == []


def test_a_held_cap_is_accepted_with_its_reason():
    findings, _, _ = caps.find_frozen(
        _pyproject(
            "# cap-hold: 2.0 drops the sync client we still use",
            '"somepkg>=1.4,<2.0",',
        ),
        _latest(somepkg="2.1.0"),
    )

    assert findings == []


def test_a_hold_with_no_reason_is_not_a_hold():
    """A bare marker is the allowlist entry nobody can evaluate."""
    findings, _, _ = caps.find_frozen(
        _pyproject("# cap-hold:", '"somepkg>=1.4,<2.0",'), _latest(somepkg="2.1.0")
    )

    assert [f.kind for f in findings] == ["hold-without-reason"]


def test_a_hold_that_holds_nothing_back_is_stale():
    """Upstream has not shipped past the cap (or the cap was raised) — the
    reason is no longer true and the marker would outlive it."""
    findings, _, _ = caps.find_frozen(
        _pyproject("# cap-hold: waiting on 2.0 fixes", '"somepkg>=1.4,<2.0",'),
        _latest(somepkg="1.9.0"),
    )

    assert [f.kind for f in findings] == ["stale-hold"]


def test_a_hold_does_not_leak_onto_the_next_requirement():
    """Only the comment block directly above counts."""
    findings, _, _ = caps.find_frozen(
        _pyproject(
            "# cap-hold: deliberate",
            '"held>=1,<2",',
            '"other>=1,<2",',
        ),
        _latest(held="3.0", other="3.0"),
    )

    assert [(f.kind, f.name) for f in findings] == [("frozen", "other")]


def test_a_package_listed_twice_is_reported_once():
    """`pydantic` and `pydantic[email]` share one cap."""
    findings, checked, _ = caps.find_frozen(
        _pyproject('"pydantic>=2.8,<3.0",', '"pydantic[email]>=2.8,<3.0",'), _latest(pydantic="3.1")
    )

    assert len(findings) == 1
    assert checked == 1


def test_optional_dependencies_are_checked_too():
    """Where `pytest<9` and `pytest-asyncio<1.0` were hiding."""
    text = '[project]\nname = "x"\ndependencies = []\n[project.optional-dependencies]\ndev = [\n    "pytest>=8.0,<9.0",\n]\n'

    findings, _, _ = caps.find_frozen(text, _latest(pytest="9.1.1"))

    assert [f.name for f in findings] == ["pytest"]


def test_a_prerelease_is_not_the_latest_release():
    """PyPI's `info.version` is the latest STABLE; a pre-release sitting past the
    cap must not trip it."""
    findings, _, _ = caps.find_frozen(_pyproject('"x>=1,<2",'), _latest(x="2.0.0rc1"))

    assert findings == []


def test_nothing_answered_is_not_a_clean_bill(tmp_path, monkeypatch, capsys):
    """If PyPI answered for nothing, "no findings" means "nothing checked"."""
    path = tmp_path / "pyproject.toml"
    path.write_text(_pyproject('"x>=1,<2",'))
    monkeypatch.setattr(caps, "pypi_latest", lambda name: None)

    assert caps.main(["prog", str(path)]) == 2
    assert "nothing was checked" in capsys.readouterr().out


@pytest.mark.parametrize("latest,expected", [("1.5", 0), ("2.0", 1)])
def test_the_exit_code_is_the_verdict(tmp_path, monkeypatch, latest, expected):
    path = tmp_path / "pyproject.toml"
    path.write_text(_pyproject('"x>=1,<2",'))
    monkeypatch.setattr(caps, "pypi_latest", lambda name: Version(latest))

    assert caps.main(["prog", str(path)]) == expected


def test_this_repos_holds_all_carry_reasons():
    """Offline, so it can run on every PR: whatever PyPI says, a hold in the
    real pyproject.toml must say why."""
    text = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text()

    findings, _, _ = caps.find_frozen(text, lambda name: None)

    assert [f for f in findings if f.kind == "hold-without-reason"] == []
