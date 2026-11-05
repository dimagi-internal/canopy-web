"""Refresh tests/fixtures/canopy_cli_routes.json: every route the canopy CLI calls here.

    uv run python scripts/refresh_canopy_cli_routes.py                     # canopy's main, from GitHub
    uv run python scripts/refresh_canopy_cli_routes.py --canopy ../canopy  # a local checkout

The list is canopy's own extractor output (`orchestrator.canopy_web_routes routes`,
stdlib only), run against canopy's source. `tests/test_canopy_cli_routes_exist.py`
fails a PR here that removes or renames a route on it. Refresh after canopy starts
calling a new route, or when that test names a call canopy has since dropped.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = "https://github.com/dimagi-internal/canopy.git"
FIXTURE = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "canopy_cli_routes.json"
_RUN = ("import sys; sys.path.insert(0, sys.argv[1]); "
        "from orchestrator.canopy_web_routes import main; main(['routes'])")


def extract(checkout: Path) -> dict:
    out = subprocess.run([sys.executable, "-c", _RUN, str(checkout / "src")], cwd=checkout,
                         check=True, capture_output=True, text=True).stdout
    data = json.loads(out)
    commit = subprocess.run(["git", "-C", str(checkout), "rev-parse", "HEAD"],
                            capture_output=True, text=True).stdout.strip()
    return {"source": {"repo": "dimagi-internal/canopy", "commit": commit}, **data}


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--canopy", type=Path, help="a canopy checkout (default: a shallow clone of main)")
    args = p.parse_args(argv)
    if args.canopy:
        data = extract(args.canopy.resolve())
    else:
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run(["git", "clone", "--quiet", "--depth", "1", REPO, tmp], check=True)
            data = extract(Path(tmp))
    FIXTURE.write_text(json.dumps(data, indent=1) + "\n")
    print(f"wrote {FIXTURE.name}: {len(data['calls'])} calls from canopy "
          f"{data['source']['commit'][:12]}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
