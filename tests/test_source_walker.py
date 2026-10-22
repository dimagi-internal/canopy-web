"""The shared source walker (tests/_source.py) reads what the source guards rely on.

Every guard over source passes vacuously on an empty walk, so this pins that the
walk is the production tree: present, and with test modules and migrations out.
"""
from __future__ import annotations

from tests._source import is_migration, is_test_file, iter_app_sources


def test_the_test_and_migration_rules():
    for rel in ("apps/x/tests/helpers.py", "apps/x/tests/test_a.py", "apps/x/test_a.py", "apps/x/tests.py",
                "apps/x/a_tests.py", "apps/x/tests/conftest.py", "tests/_source.py"):
        assert is_test_file(rel), rel
    for rel in ("apps/x/testing.py", "apps/x/services.py", "apps/tests_app/models.py"):
        assert not is_test_file(rel), rel
    assert is_migration("apps/x/migrations/0001_initial.py")
    assert not is_migration("apps/x/migrations.py")


def test_the_walk_is_the_production_tree():
    rels = [src.rel for src in iter_app_sources()]
    assert "apps/workspaces/services.py" in rels and "config/urls.py" in rels
    assert len(rels) > 400, "the guards would pass on a walk that found nothing"
    assert len(rels) == len(set(rels))
    assert not [r for r in rels if is_test_file(r) or is_migration(r)]


def test_include_and_exclude():
    with_tests = {s.rel for s in iter_app_sources(("apps/workspaces",), include_tests=True,
                                                  include_migrations=True)}
    assert any("/tests/" in r for r in with_tests) and any("/migrations/" in r for r in with_tests)
    out = {s.rel for s in iter_app_sources(("apps",), exclude=("apps/workspaces/", "apps/agents/models.py"))}
    assert not any(r.startswith("apps/workspaces/") for r in out) and "apps/agents/models.py" not in out
    assert "apps/agents/services.py" in out


def test_a_source_unpacks_and_parses_once():
    src = next(iter(iter_app_sources(("apps/workspaces",))))
    path, rel, text, tree = src
    assert path.read_text() == text and src.tree is tree
