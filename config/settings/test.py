"""
Test settings for canopy-web.

Uses SQLite in-memory database for fast test execution.
"""
from .base import *  # noqa: F401, F403

DEBUG = True

# No real OAuth client keys here: generate throwaway ones per process
# (apps/tokens/client_identity.py). A deployment never does this.
CANOPY_OAUTH_EPHEMERAL_KEYS = True

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
