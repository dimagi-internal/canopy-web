"""
Development settings for canopy-web.
"""
from .base import *  # noqa: F401, F403

DEBUG = True

# No real OAuth client keys here: generate throwaway ones per process
# (apps/tokens/client_identity.py). A deployment never does this.
CANOPY_OAUTH_EPHEMERAL_KEYS = True

# The Vite dev server (port 3000) proxies /api here, so browser POSTs carry an
# Origin of http://localhost:3000. Trust it so Django's CSRF origin check passes
# in dev (in prod the SPA is same-origin, handled by production.py).
CSRF_TRUSTED_ORIGINS = [
    "http://localhost:3000",
    "http://127.0.0.1:3000",
]
