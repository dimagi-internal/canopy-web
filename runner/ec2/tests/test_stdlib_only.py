"""cloud_runner.py must stay stdlib-only — checked against the MODULE, not the env.

The EC2 box runs cloud_runner.py with its system python3, so a dependency the
module picks up from canopy-web's environment works in CI and dies on the box.
This used to be "enforced" by running the suite in a bare env, but runner/ec2
has no pyproject.toml: `uv run` there walks up to the root project, Django is
importable, and an accidental `import django` passed CI (#1351). Running the
suite with `--no-project` would not fix it either — the tests themselves need
canopy_transcript and yaml, so the env has to carry them, and then a top-level
`import canopy_transcript` passes too.

So both checks below look at cloud_runner.py itself and hold in any env:

- it IMPORTS with site-packages disabled (`-I -S`): nothing but the stdlib on
  the path, so any non-stdlib top-level import fails here exactly as on the box;
- every import statement in it — including the lazy ones inside functions,
  which an import-time check never executes — names a stdlib module or one of
  the few non-stdlib modules the runner deliberately loads lazily, on the code
  paths that need them (_LAZY_NON_STDLIB, each with where the box gets it).
"""
from __future__ import annotations

import ast
import pathlib
import subprocess
import sys

_MODULE_PATH = pathlib.Path(__file__).resolve().parent.parent / "cloud_runner.py"

# The only non-stdlib modules cloud_runner.py may import — and only lazily,
# inside the function that needs them, never at import time. Each entry names
# where the box gets it; adding one is a deliberate act, so say that too.
_LAZY_NON_STDLIB = {
    # Fleet packages from the runner's own canopy-web checkout.
    "canopy_acp": "canopy-web checkout (packages/canopy_acp)",
    "canopy_transcript": "canopy-web checkout (packages/canopy_transcript)",
    "canopy_runner": "canopy-web checkout (runner/canopy_runner)",
    # Optional: run_over_ws() falls back to REST on ImportError.
    "websocket": "apt python3-websocket (runner.cfn.yaml UserData)",
}


def _imports(tree: ast.AST):
    """(top-level module name, lineno, runs_at_import) for every import.

    "Runs at import" means not inside a function body — an import under a
    module-level `try:` or `if` executes on import just like a bare one."""
    in_function = {
        id(inner)
        for fn in ast.walk(tree)
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))
        for inner in ast.walk(fn)
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # relative import — cannot occur in a script, but don't misread it
                continue
            names = [node.module or ""]
        else:
            continue
        for name in names:
            yield name.split(".")[0], node.lineno, id(node) not in in_function


def test_imports_with_only_the_stdlib_on_the_path():
    # -I: ignore PYTHON* env vars and the script dir; -S: no site, so no
    # site-packages — the venv's Django, yaml and canopy_* are all invisible.
    result = subprocess.run(
        [
            sys.executable, "-I", "-S", "-c",
            "import runpy, sys; runpy.run_path(sys.argv[1], run_name='cloud_runner_import_check')",
            str(_MODULE_PATH),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, (
        "cloud_runner.py no longer imports with only the stdlib — the EC2 box's "
        f"python3 will fail the same way:\n{result.stderr}"
    )


def test_every_import_is_stdlib_or_a_named_lazy_fleet_package():
    tree = ast.parse(_MODULE_PATH.read_text(), filename=str(_MODULE_PATH))
    stdlib = sys.stdlib_module_names
    bad = []
    for name, lineno, at_module_level in _imports(tree):
        if name in stdlib or name == "__future__":
            continue
        if name in _LAZY_NON_STDLIB and not at_module_level:
            continue
        where = "runs at import" if at_module_level else "inside a function"
        bad.append(f"line {lineno}: import {name} ({where})")
    assert not bad, (
        "cloud_runner.py must stay stdlib-only; the only exceptions are lazy imports "
        f"of {sorted(_LAZY_NON_STDLIB)} (see _LAZY_NON_STDLIB for where the box "
        "gets each):\n" + "\n".join(bad)
    )
