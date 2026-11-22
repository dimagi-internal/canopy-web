"""No migration may depend on another app's `__latest__`.

`("agents", "__latest__")` resolves to whatever that app's newest migration is at the time
`migrate` runs. Once the depending migration is applied in production, the next migration
added to the other app becomes one of its dependencies after the fact, and `migrate` refuses
with InconsistentMigrationHistory -- which is how the 2026-11-22 deploy of agents/0136 failed
(canopy_sessions/0072 depended on agents `__latest__`). Pin the dependency to a named migration.
"""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_no_migration_depends_on_latest():
    offenders = []
    for path in sorted(ROOT.glob("apps/*/migrations/[0-9]*.py")):
        if "__latest__" in path.read_text(encoding="utf-8"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and node.value == "__latest__":
                    offenders.append(str(path.relative_to(ROOT)))
                    break
    assert not offenders, f"pin these dependencies to a named migration: {offenders}"
