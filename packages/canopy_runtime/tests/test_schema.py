"""Plain-pytest suite for the canopy_runtime schema (no Django, no DB)."""
from __future__ import annotations

from pathlib import Path

import pytest
from canopy_runtime import RuntimeSpec, load_runtime_yaml
from pydantic import ValidationError

EXAMPLE = Path(__file__).resolve().parents[1] / "canopy_runtime" / "example_runtime.yaml"


def test_empty_spec_is_valid_with_defaults():
    spec = RuntimeSpec()
    assert spec.version == 1
    assert spec.engine == "any"
    assert spec.plugins == spec.mcp == spec.tools == spec.secrets == spec.preflight == []


def test_shipped_example_yaml_validates():
    spec = load_runtime_yaml(EXAMPLE.read_text())
    assert spec.engine == "any"
    assert {p.name for p in spec.plugins} == {"canopy", "ace", "echo"}
    assert {s.name for s in spec.secrets} >= {"canopy-pat", "claude-oauth-token"}
    # secrets declare where their value lands (env var / file path).
    claude = next(s for s in spec.secrets if s.name == "claude-oauth-token")
    assert claude.env == "CLAUDE_CODE_OAUTH_TOKEN"
    assert any(c.name == "claude-authed" for c in spec.preflight)


def test_unknown_top_level_key_is_rejected():
    # A typo'd key must fail the agent's PR, not be silently dropped.
    with pytest.raises(ValidationError):
        load_runtime_yaml("plugin:\n  - name: canopy\n")  # 'plugin' not 'plugins'


def test_unknown_nested_key_is_rejected():
    with pytest.raises(ValidationError):
        RuntimeSpec.model_validate({"plugins": [{"name": "canopy", "srce": "x"}]})


def test_engine_is_constrained():
    with pytest.raises(ValidationError):
        RuntimeSpec.model_validate({"engine": "mdash"})
    for good in ("emdash", "cloud_p", "any"):
        assert RuntimeSpec.model_validate({"engine": good}).engine == good


def test_plugin_requires_a_name():
    with pytest.raises(ValidationError):
        RuntimeSpec.model_validate({"plugins": [{"source": "x"}]})
    with pytest.raises(ValidationError):
        RuntimeSpec.model_validate({"plugins": [{"name": ""}]})


@pytest.mark.parametrize(
    "leaked",
    [
        "TOKEN=supersecret",        # an assignment — clearly a value
        "some value with spaces",   # spaces — not a slug
        "x" * 121,                  # implausibly long for a reference name
    ],
)
def test_secret_values_are_rejected(leaked):
    # A secret's `name` is a reference, not a value; the '='/space/length cases are
    # unambiguous leaks and must fail validation.
    with pytest.raises(ValidationError):
        RuntimeSpec.model_validate({"secrets": [{"name": leaked}]})


def test_secret_reference_names_pass_and_carry_destinations():
    spec = RuntimeSpec.model_validate(
        {"secrets": [
            {"name": "canopy-pat", "env": "CANOPY_PAT"},
            {"name": "gog-token", "optional": True},
        ]}
    )
    assert [s.name for s in spec.secrets] == ["canopy-pat", "gog-token"]
    assert spec.secrets[0].env == "CANOPY_PAT"
    assert spec.secrets[1].optional is True


def test_secret_requires_a_name():
    with pytest.raises(ValidationError):
        RuntimeSpec.model_validate({"secrets": [{"env": "X"}]})


def test_load_rejects_non_mapping_top_level():
    with pytest.raises(ValueError):
        load_runtime_yaml("- just\n- a\n- list\n")


# Real agents' runtime.yaml, copied verbatim (ACE's from ace#2908, echo's from its
# repo). A schema that rejects the files agents actually ship is a schema nobody can
# use: ACE's failed with 48 errors on `op`, `value`, `local_only` and tool `optional`.
FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.mark.parametrize("name", ["ace-runtime.yaml", "echo-runtime.yaml"])
def test_real_agent_runtime_yaml_validates(name):
    spec = load_runtime_yaml((FIXTURES / name).read_text())
    assert spec.secrets


def test_ace_runtime_carries_its_sources_capabilities_and_optional_tools():
    spec = load_runtime_yaml((FIXTURES / "ace-runtime.yaml").read_text())
    by = {s.name: s for s in spec.secrets}
    assert by["canopy-pat"].op == "op://Agent-Ace/canopy-pat/credential"
    assert by["connect-base-url"].value == "https://connect.dimagi.com"
    assert by["ace-web-pat-token"].local_only is True
    assert {t.name for t in spec.tools if t.optional} == {"java", "adb", "maestro"}
    assert [c.manifest for c in spec.capabilities] == ["infra/capabilities/android-emulator/capability.yaml"]


@pytest.mark.parametrize("secret", [
    {"name": "x", "op": "op://A/b/c", "value": "v"},       # one source, not two
    {"name": "x", "op": "Agent-Ace/b/c"},                   # op is an op:// reference
    {"name": "x", "local_only": True, "op": "op://A/b/c"},  # local_only has no shared source
])
def test_secret_source_rules(secret):
    with pytest.raises(ValidationError):
        RuntimeSpec.model_validate({"secrets": [secret]})
