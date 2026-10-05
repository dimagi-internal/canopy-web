"""Django settings for canopy-web deployed to the connect-labs AWS environment.

canopy-web is served at the ROOT of https://canopy.dimagi.com (since 2026-10-05),
through the shared labs ALB (a host-header rule). It was the third tenant on
labs.connect.dimagi.com, at /canopy — connect-labs at the root, ace-web at /ace —
and that old address still reaches this container: machine traffic (API, OAuth,
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

# canopy's protocol identity stays on the OLD address until each connected site
# is re-pointed: ace-web pins CANOPY_CLIENT_ID to …/canopy/oauth/client.json and
# signs its visitor assertions for this audience. Everything it names is still
# served there. Moving it is a coordinated change with every connected site.
CANOPY_IDENTITY_BASE_URL = env("CANOPY_IDENTITY_BASE_URL", default="https://labs.connect.dimagi.com/canopy")

# canopy's live probe of its OWN host half (the `canopy-web` Connected site):
# the dedicated, non-admin user tokens/0026_probe_user creates. Non-secret, and
# ON here so the probe runs once deployed (apps/tokens/live_probe.py).
CANOPY_HOST_PROBE_USERNAME = env("CANOPY_HOST_PROBE_USERNAME", default="canopy-probe")

# The labs account's verified SES domain (see apps/common/email.py). Never a
# domain prod Connect sends from: labs' reputation must not touch anyone else's.
DEFAULT_FROM_EMAIL = env("DEFAULT_FROM_EMAIL", default="Canopy <noreply@labs.connect.dimagi.com>")

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
# Channel layer = InMemory, NOT Redis, and this is deliberate.
#
# This service runs a SINGLE ECS task with a SINGLE uvicorn process (DesiredCount=1,
# no --workers). Every WebSocket consumer AND every group-send publisher (turn
# enqueue → wake, chat → cancel) lives in that one process, so an in-process
# layer is all the coordination that's needed — and it is the layer the Channels
# docs recommend for a single process.
#
# The Redis channel layer (channels_redis) actively BROKE long-lived WebSockets
# here: its consumer receive loop does a blocking read against the shared
# ElastiCache, and on an idle connection that read raises
# `redis.exceptions.TimeoutError: Timeout reading from …` up through
# channels.utils.await_many_dispatch, which tears the socket down. That produced
# erratic ~5-10s disconnects on every realtime surface (runner control channel,
# supervisor + turn tails) that looked like a proxy idle-timeout but was not — the
# shared ALB holds long connections fine (connect-labs' long-running workflows
# prove it). Redis bought us nothing here (nothing to coordinate across) and cost
# us the whole feature.
#
# SCALING CAVEAT: InMemory does not cross processes. If this service ever runs more
# than one web task (or uvicorn --workers > 1), a runner connected to task A would
# not receive a wake published by task B. At that point restore a Redis channel
# layer — but configure it with connection health so idle reads don't kill sockets:
#   "hosts": [{"address": _REDIS_URL, "health_check_interval": 30,
#             "socket_keepalive": True, "retry_on_timeout": True}]
# Redis stays the Django CACHE backend above (request-scoped reads, never a
# long-lived blocking pop — unaffected by this).
CHANNEL_LAYERS = {"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}}

# Chat sends execute for REAL on labs (not the inline dev stub): a chat Session turn
# stays QUEUED for a session-capable runner (e.g. the laptop emdash daemon), which
# drives the agent's emdash session and bridges the reply back to the ledger — so the
# website streams the actual agent response and you can continue a session from your
# phone. If no session-capable runner is online, the turn simply waits (rather than
# getting an instant fake reply). See apps/canopy_sessions/executor.py + runner/canopy_runner
# chat_bridge/execute_chat_turn.
CHAT_STUB_EXECUTOR = False
