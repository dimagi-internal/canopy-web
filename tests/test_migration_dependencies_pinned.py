"""No migration may depend on another app's `__latest__`.

`("agents", "__latest__")` resolves to that app's newest migration at the time `migrate` runs.
Once the depending migration is applied in production, every migration later added to the
other app becomes its dependency after the fact, and `migrate` refuses with
InconsistentMigrationHistory. A dependency therefore names a migration.
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
