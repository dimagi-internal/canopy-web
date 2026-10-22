"""
Test settings for canopy-web.

Uses SQLite in-memory database for fast test execution.
"""
from .base import *  # noqa: F401, F403

DEBUG = True

# No real OAuth client keys here: generate throwaway ones per process
# (apps/tokens/client_identity.py). A deployment never does this.
CANOPY_OAUTH_EPHEMERAL_KEYS = True
# The scheduled live probe starts a background thread; never in a test run.

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
    }
}

# Speed up password hashing in tests
PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.MD5PasswordHasher",
]

# Default tests to unauthenticated access so existing suites keep passing.
# Auth-specific tests override this per-test via settings().
REQUIRE_AUTH = False

# Memory's rollout gate (base.py): open in tests; tests of the gate itself narrow it.
HCP_PEOPLE = ["*"]
HCP_CONTACT_OPT_IN = True

# MCP Apps: no background index refresh threads in tests (a thread outlives the
# test's transaction); tests that want it call `mcp_apps_views._refresh` directly.
MCP_APPS_BACKGROUND_REFRESH = False

# The capability sweep has no clock of its own in tests: they call sweep() /
# sweep_tick() directly, and an ASGI lifespan a test enters must not start one.
CAPABILITY_SWEEP_INTERVAL_S = 0
# Likewise the Slack channel-ingest reader: tests call `ingest.tick()` / `advance()`.
SLACK_INGEST_TICK_S = 0
# Likewise the canopy-pat rotation: tests call `pat_rotation.tick()` / `rotate()`.
CANOPY_PAT_ROTATION_TICK_S = 0
CANOPY_SCHEDULE_TICK_S = 0
# And the one off-request clock (apps/common/ticks.py) they all run on: tests call
# a job's function, or `ticks.tick()`, directly.
TICK_BASE_S = 0
