"""Django settings for canopy-web deployed to the connect-labs AWS environment.

canopy-web is served at the ROOT of https://canopy.dimagi.com,
through the shared labs ALB (a host-header rule). The old address
labs.connect.dimagi.com/canopy still reaches this container: machine traffic (API, OAuth,
well-known, health; WebSockets) keeps working there, and browser pages are
redirected to the new address (apps/common/legacy_prefix.py).

Inherits production security; configures for ALB TLS termination, the legacy
/canopy prefix, the shared RDS (canopy_web DB), and the shared ElastiCache Redis.
"""
from .production import *  # noqa: F401, F403

import environ  # noqa: E402

env = environ.Env()

# ALB terminates TLS at the edge; the ALB -> container hop is plain HTTP. Trust
# X-Forwarded-Proto so request.scheme is "https" and OAuth callback URLs are
# built as https:// (Google rejects http:// redirect_uris).
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_SSL_REDIRECT = False  # ALB handles the redirect

# Permissive behind the ALB (the only thing reaching this service is the ALB,
# which routes canopy.dimagi.com and labs' /canopy/* here) — matches canopy's production default and
# lets the ALB IP-based health check (Host: <container-ip>) through. Host-header
# attacks are mooted by the ALB boundary; CSRF is still pinned via
# CSRF_TRUSTED_ORIGINS below.
ALLOWED_HOSTS = ["*"]

# Served at the root: Django generates root URLs. The OLD address arrives with a
# /canopy prefix, which CANOPY_STRIP_PREFIX (config/asgi.py) strips on the way in
# and apps/common/legacy_prefix.py puts back for the URLs generated in reply.
# Setting FORCE_SCRIPT_NAME again would serve canopy under /canopy everywhere.
FORCE_SCRIPT_NAME = env("FORCE_SCRIPT_NAME", default="") or None

# The address people visit — every link canopy sends (invites, Slack, emails,
# OAuth callbacks, readiness-drill reports) is built from this.
CANOPY_PUBLIC_BASE_URL = env("CANOPY_PUBLIC_BASE_URL", default="https://canopy.dimagi.com")

# canopy's protocol identity — its CIMD client_id, the audience a site signs
# visitor assertions for, its own issuer and MCP resource. ace-web and
# connect-labs pin CANOPY_CLIENT_ID and the audience, so moving
# it is a coordinated change with every host site.
CANOPY_IDENTITY_BASE_URL = env("CANOPY_IDENTITY_BASE_URL", default="https://canopy.dimagi.com")

# Addresses canopy has left. Machine traffic there is still served
# (apps/common/legacy_prefix.py); this lets the Slack sync recognise the
# commands and webhooks it registered under them and move them.
CANOPY_FORMER_BASE_URLS = ["https://labs.connect.dimagi.com/canopy"]

# canopy's live probe of its OWN host half (the `canopy-web` host site):
# the dedicated, non-admin user tokens/0026_probe_user creates. Non-secret, and
# ON here so the probe runs once deployed (apps/host_grants/live_probe.py).
CANOPY_HOST_PROBE_USERNAME = env("CANOPY_HOST_PROBE_USERNAME", default="canopy-probe")

# canopy's own verified SES domain (deploy/aws/canopy-email.cfn.yaml; see
# apps/common/email.py). Never a domain prod Connect sends from: labs' reputation
# must not touch anyone else's.
DEFAULT_FROM_EMAIL = env("DEFAULT_FROM_EMAIL", default="Canopy <noreply@canopy.dimagi.com>")

LOGIN_URL = "/accounts/google/login/"

# The names stay tenant-unique (they were chosen to coexist with connect-labs and
# ace-web on the shared labs hostname, and the SPA bundle reads the CSRF name via
# VITE_CSRF_COOKIE_NAME); the PATH is the root now that canopy owns its host.
SESSION_COOKIE_NAME = "sessionid_canopy"
CSRF_COOKIE_NAME = "csrftoken_canopy"
SESSION_COOKIE_PATH = "/"
CSRF_COOKIE_PATH = "/"

CSRF_TRUSTED_ORIGINS = ["https://canopy.dimagi.com", "https://labs.connect.dimagi.com"]

STATIC_URL = "/static/"

# Shared RDS (a dedicated canopy_web database on labs-jj-postgres).
DATABASES = {"default": env.db("DATABASE_URL")}

# Shared ElastiCache Redis. canopy uses a dedicated DB index (REDIS_URL ends in
# /1) so its keyspace doesn't collide with ace/connect-labs. The Channels layer
# (CHANNEL_LAYERS) is added in W4.1 once `channels` is a dependency; this cache
# config is the same endpoint and is safe to ship now.
_REDIS_URL = env("REDIS_URL", default="")
if _REDIS_URL:
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.redis.RedisCache",
            "LOCATION": _REDIS_URL,
        }
    }
# Channel layer: Redis pub/sub, so more than one web task or uvicorn worker can serve
# sockets. Why pub/sub and not channels_redis's default (whose blocking reads tore
# sockets down on this ElastiCache), and the prefix, are in config/channel_layers.py.
# CHANNEL_LAYER=memory goes back to the in-process layer, which only works with ONE
# process. Redis stays the Django cache backend above either way.
from config.channel_layers import channel_layers  # noqa: E402

CHANNEL_LAYERS = channel_layers(_REDIS_URL, env("CHANNEL_LAYER", default=""))

# Chat sends execute for REAL on labs (not the inline dev stub): a chat Session turn
# stays QUEUED for a session-capable runner (e.g. the laptop emdash daemon), which
# drives the agent's emdash session and bridges the reply back to the ledger — so the
# website streams the actual agent response and you can continue a session from your
# phone. If no session-capable runner is online, the turn simply waits (rather than
# getting an instant fake reply). See apps/canopy_sessions/executor.py + runner/canopy_runner
# chat_bridge/execute_chat_turn.
CHAT_STUB_EXECUTOR = False
