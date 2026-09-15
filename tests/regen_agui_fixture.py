"""Rewrite the AG-UI cross-language fixture.

    uv run python -m tests.regen_agui_fixture

The fixture is the only artifact the Python projection and its TypeScript
inverse both read, so it is generated rather than hand-written — a hand-kept
copy would drift silently, which is the exact failure it exists to catch.
"""

import json

from tests.test_agui_projection import FIXTURE, _build_fixture

if __name__ == "__main__":
    FIXTURE.write_text(json.dumps(_build_fixture(), indent=2) + "\n")
    print(f"wrote {FIXTURE}")
