"""Django settings for canopy-web deployed to the connect-labs AWS environment.

canopy-web runs as the third tenant on labs.connect.dimagi.com:
  - connect-labs (the primary) at the root
  - ace-web at /ace
  - canopy-web at /canopy   ← this

Inherits production security; configures for ALB TLS termination, the /canopy
path prefix, tenant-unique path-scoped cookies, the shared RDS (canopy_web DB),
and the shared ElastiCache Redis (cache now; Channels layer lands with W4).
Mirrors ace-web's connectlabs.py.
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
# which routes just /canopy/* here) — matches canopy's production default and
# lets the ALB IP-based health check (Host: <container-ip>) through. Host-header
# attacks are mooted by the ALB boundary; CSRF is still pinned via
# CSRF_TRUSTED_ORIGINS below.
ALLOWED_HOSTS = ["*"]

# Served under /canopy on the shared ALB.
FORCE_SCRIPT_NAME = env("FORCE_SCRIPT_NAME", default="/canopy")

# The labs public URL, path-prefixed — readiness-drill report callbacks are built
# from this (apps/harness/services.py::start_drill).
CANOPY_PUBLIC_BASE_URL = env("CANOPY_PUBLIC_BASE_URL", default="https://labs.connect.dimagi.com/canopy")

# `@login_required`/allauth build redirects from LOGIN_URL via resolve_url,
# which does NOT prepend FORCE_SCRIPT_NAME — so it must already carry /canopy,
# else an anonymous hit redirects to a path owned by a sibling labs tenant.
LOGIN_URL = "/canopy/accounts/google/login/"

# Tenant-unique, path-scoped cookies so state doesn't collide with or leak to
# the sibling tenants (connect-labs root, ace at /ace) on the shared hostname.
SESSION_COOKIE_NAME = "sessionid_canopy"
CSRF_COOKIE_NAME = "csrftoken_canopy"
SESSION_COOKIE_PATH = "/canopy/"
CSRF_COOKIE_PATH = "/canopy/"

CSRF_TRUSTED_ORIGINS = ["https://labs.connect.dimagi.com"]

# Static served under the prefix (the SPA's own assets are emitted by Vite with
# base=/canopy/; this covers Django/admin/staticfiles).
STATIC_URL = "/canopy/static/"

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
# enqueue → wake, chat → interject) lives in that one process, so an in-process
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
