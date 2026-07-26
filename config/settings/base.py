"""
Base Django settings for canopy-web.

Common settings shared across all environments.
"""
import sys
from pathlib import Path

import environ

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent.parent

# Add canopy submodule to sys.path if it exists
CANOPY_DIR = BASE_DIR / "canopy"
if CANOPY_DIR.exists():
    sys.path.insert(0, str(CANOPY_DIR))

# Environment
env = environ.Env(
    DEBUG=(bool, False),
    ALLOWED_HOSTS=(list, ["localhost", "127.0.0.1"]),
)

# Read .env file if it exists
env_file = BASE_DIR / ".env"
if env_file.exists():
    env.read_env(str(env_file))

# SECURITY WARNING: keep the secret key used in production secret!
# This is a shared, deliberately-insecure default so a fresh checkout can run
# `manage.py runserver` with zero setup — localhost signs nothing worth stealing,
# so devs do NOT each need their own key. It is NOT a real secret: the `django-
# insecure-` prefix is Django's own marker for exactly this. Production reads
# SECRET_KEY from the environment and production.py hard-fails if it ever sees
# this default (see the assertion there), so prod can never fall back to it.
INSECURE_DEV_SECRET_KEY = "django-insecure-change-me-in-production"
SECRET_KEY = env("SECRET_KEY", default=INSECURE_DEV_SECRET_KEY)

# SECURITY WARNING: don't run with debug turned on in production!
DEBUG = env("DEBUG")

ALLOWED_HOSTS = env("ALLOWED_HOSTS")

# Path to the canopy plugin source (skills/ agents/ commands/) read by
# apps.system to render the capability catalog at /system. In production the
# Docker build clones jjackson/canopy into CANOPY_DIR; for local dev, point this
# at your installed plugin cache, e.g.
#   CANOPY_PLUGIN_PATH=~/.claude/plugins/cache/canopy/canopy/<version>
# When the path is absent the catalog renders empty with a warning (never 500s).
#
# The jjackson/canopy marketplace repo NESTS the plugin under plugins/canopy/;
# a bare plugin checkout (the cache) has skills/ at its root. Auto-detect both.
def _resolve_canopy_plugin_path() -> str:
    explicit = env.str("CANOPY_PLUGIN_PATH", default="")
    if explicit:
        return explicit
    for cand in (CANOPY_DIR / "plugins" / "canopy", CANOPY_DIR):
        if (cand / "skills").is_dir():
            return str(cand)
    return str(CANOPY_DIR)


CANOPY_PLUGIN_PATH = _resolve_canopy_plugin_path()

# Application definition
INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.sites",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # Third-party
    "channels",
    "corsheaders",
    "allauth",
    "allauth.account",
    "allauth.socialaccount",
    "allauth.socialaccount.providers.google",
    # Local apps
    "apps.common",
    "apps.projects",
    "apps.issues",
    "apps.walkthroughs",
    "apps.tokens",
    "apps.reviews",
    "apps.runs",
    "apps.shareouts",
    "apps.mcp",
    "apps.session_sharing",
    "apps.push",
    "apps.agents",
    "apps.agent_runs",
    "apps.workspaces",
    "apps.timeline",
    "apps.system",
    "apps.harness",
    "apps.feedback",
    "apps.realtime",
    "apps.canopy_sessions",
]

MIDDLEWARE = [
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "apps.tokens.middleware.BearerTokenAuthMiddleware",  # PAT bearer auth (after AuthenticationMiddleware, before LoginRequired)
    "apps.api.tenancy.WorkspaceResolveMiddleware",  # resolve /api/w/{ws}/ + flat-route compat shim (needs request.user)
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "allauth.account.middleware.AccountMiddleware",
    "apps.common.middleware.LoginRequiredMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

ASGI_APPLICATION = "config.asgi.application"

# --- Channels realtime layer (apps/realtime) -------------------------
# In-memory by default so a fresh checkout / single-process runserver works with
# zero infra. When REDIS_URL is set (docker-compose, prod) use the Redis layer so
# fan-out crosses processes/containers. Tests keep the in-memory layer.
_CHANNELS_REDIS_URL = env("REDIS_URL", default="")
if _CHANNELS_REDIS_URL:
    CHANNEL_LAYERS = {
        "default": {
            "BACKEND": "channels_redis.core.RedisChannelLayer",
            # prefix namespaces channel keys away from the Django cache on the
            # same Redis DB (it does not survive a FLUSHDB, but cache.clear() is
            # not on a prod path).
            "CONFIG": {"hosts": [_CHANNELS_REDIS_URL], "prefix": "canopy:realtime:"},
        }
    }
else:
    CHANNEL_LAYERS = {"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}}

# Database
DATABASES = {
    "default": env.db("DATABASE_URL", default="postgres://localhost:5432/canopy_web"),
}

# Password validation
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# Internationalization
LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

# Static files (CSS, JavaScript, Images)
STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

# WhiteNoise serves the built frontend, and its MediaTypes map is its own — it
# never consults Python's mimetypes, and it has no .webmanifest entry. Without
# this the PWA manifest goes out as application/octet-stream. Chrome tolerates
# that; the spec and Lighthouse don't, and the manifest is the thing that makes
# /supervisor installable.
WHITENOISE_MIMETYPES = {".webmanifest": "application/manifest+json"}

# Frontend SPA build output (served by catch-all view; WhiteNoise handles assets)
FRONTEND_DIST_DIR = BASE_DIR / "frontend" / "dist"

# Default primary key field type
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Authentication
SITE_ID = 1
AUTHENTICATION_BACKENDS = [
    "django.contrib.auth.backends.ModelBackend",
    "allauth.account.auth_backends.AuthenticationBackend",
]

LOGIN_URL = "/accounts/google/login/"
LOGIN_REDIRECT_URL = "/"
ACCOUNT_LOGOUT_REDIRECT_URL = "/"

# allauth
ACCOUNT_EMAIL_VERIFICATION = "none"
# Identity comes from Google only: close the local username/password signup
# form that allauth.urls otherwise exposes at /accounts/signup/ (it bypasses
# the social adapter's domain/invite gate entirely).
ACCOUNT_ADAPTER = "apps.common.auth_adapter.CustomAccountAdapter"
# NB: the two settings below are allauth>=65 names and are INERT on the
# pinned 0.63.x. Kept for the eventual upgrade; they are NOT what closes
# signup today — ACCOUNT_ADAPTER above is.
ACCOUNT_LOGIN_METHODS = {"email"}
ACCOUNT_SIGNUP_FIELDS = ["email*"]
SOCIALACCOUNT_LOGIN_ON_GET = True
SOCIALACCOUNT_ADAPTER = "apps.common.auth_adapter.CustomSocialAccountAdapter"
SOCIALACCOUNT_PROVIDERS = {
    "google": {
        "APP": {
            "client_id": env("GOOGLE_OAUTH_CLIENT_ID", default=""),
            "secret": env("GOOGLE_OAUTH_CLIENT_SECRET", default=""),
            "key": "",
        },
        "SCOPE": ["profile", "email"],
        "AUTH_PARAMS": {"access_type": "online"},
    }
}

# Restrict Google login to this email domain (empty = allow all)
AUTH_ALLOWED_EMAIL_DOMAIN = env("AUTH_ALLOWED_EMAIL_DOMAIN", default="dimagi.com")

# Whether LoginRequiredMiddleware enforces auth. Default on; toggle off during rollout.
REQUIRE_AUTH = env.bool("REQUIRE_AUTH", default=True)

# Default lifetime for a newly minted Personal Access Token, in days. Applies
# only when the mint call doesn't pass an explicit ttl_days; 0 there means
# "never expires". Pre-existing tokens are grandfathered (expires_at NULL) —
# see apps/tokens/migrations/0003.
PAT_DEFAULT_TTL_DAYS = env.int("PAT_DEFAULT_TTL_DAYS", default=180)

# Rolling session: refresh the expiry on every request rather than only at login.
# Django's default (False) sets the 2-week expiry AT LOGIN and never extends it,
# so an installed PWA would log you out every fortnight no matter how often you
# opened it. Costs one session write per request; fine at this scale.
SESSION_SAVE_EVERY_REQUEST = True

# --- Walkthrough sharing (apps/walkthroughs) ---
# When False, all /api/walkthroughs/ endpoints 404 (rollout flag).
WALKTHROUGHS_ENABLED = env.bool("WALKTHROUGHS_ENABLED", default=True)

# Google Service Account JSON for the Drive that stores walkthrough
# files. Empty string disables uploads/downloads (returns 500 with
# code="drive-not-configured" — same affordance as ace-web).
CANOPY_DRIVE_SA_KEY_JSON = env("CANOPY_DRIVE_SA_KEY_JSON", default="")

# ID of the shared-drive folder under which "walkthroughs/<uuid>/"
# subfolders are created.
CANOPY_DRIVE_ROOT_FOLDER_ID = env("CANOPY_DRIVE_ROOT_FOLDER_ID", default="")

# Max upload size in bytes for a single walkthrough file. 75 MB covers
# small videos and large HTML decks.
WALKTHROUGH_MAX_UPLOAD_BYTES = env.int(
    "WALKTHROUGH_MAX_UPLOAD_BYTES", default=75 * 1024 * 1024,
)

# --- Chat attachments (apps/canopy_sessions) -------------------------
# S3 bucket holding chat attachment bytes. Empty string disables the feature
# entirely: uploads 503 with code="attachments-not-configured" rather than
# half-working, so a dev box without AWS credentials fails loudly instead of
# writing rows that point at bytes which do not exist.
#
# Access is granted by a BUCKET policy naming the ECS task role, not by an IAM
# policy on that role: the task role (labs-jj-ecs-task-role) belongs to the
# shared labs platform and is only a *parameter* to our CloudFormation stack, so
# we cannot attach to it. See deploy/aws/canopy-web.cfn.yaml.
CANOPY_ATTACHMENTS_BUCKET = env("CANOPY_ATTACHMENTS_BUCKET", default="")

# Max bytes for one attachment. Deliberately far below the walkthrough cap: this
# is the phone-screenshot path, and the bytes stream through the web dyno.
ATTACHMENT_MAX_UPLOAD_BYTES = env.int(
    "ATTACHMENT_MAX_UPLOAD_BYTES", default=10 * 1024 * 1024,
)

# An allowlist, never a denylist: the bytes are handed to an agent that will
# open them, and the browser renders them inline. Images only for now — the
# stated use case is "show the agent a screenshot".
ATTACHMENT_ALLOWED_CONTENT_TYPES = env.list(
    "ATTACHMENT_ALLOWED_CONTENT_TYPES",
    default=["image/png", "image/jpeg", "image/gif", "image/webp"],
)

# --- Agent-run Drive backing (apps/agent_runs) -----------------------
# Drive-as-truth run store for agents whose runs live in a Google Drive
# tree (e.g. ACE opps under ACE/<slug>/runs/<run-id>/). All knobs are
# optional and empty by default: with nothing set, every agent resolves
# to the DB-backed run store (apps.agent_runs.resolver.get_run_store) and
# nothing here is touched — so a deploy without Drive creds is unaffected.
#
# Credentials (first non-empty wins; see
# apps.agent_runs.drive.google_client._load_credentials):
#   AGENT_RUNS_DRIVE_SA_KEY_JSON  — inline service-account JSON, or
#   AGENT_RUNS_DRIVE_SA_KEY_PATH  — path to a service-account JSON file, or
#   CANOPY_DRIVE_SA_KEY_JSON      — the shared canopy Drive SA (fallback).
AGENT_RUNS_DRIVE_SA_KEY_JSON = env("AGENT_RUNS_DRIVE_SA_KEY_JSON", default="")
AGENT_RUNS_DRIVE_SA_KEY_PATH = env("AGENT_RUNS_DRIVE_SA_KEY_PATH", default="")

# Which agents are Drive-backed, and the Drive root folder each one's runs
# live under (the folder that contains `runs/<run-id>/` and optionally
# `opp.yaml`). JSON map of {agent_slug: drive_root_folder_id}. Empty = no
# agent is Drive-backed. Example:
#   AGENT_RUNS_DRIVE_ROOTS='{"ace": "0AbCdEf...root-folder-id"}'
AGENT_RUNS_DRIVE_ROOTS = env.json("AGENT_RUNS_DRIVE_ROOTS", default={})

# Machine-caller authentication: see apps/tokens/ for Personal Access
# Tokens. Mint with `manage.py create_token --email X --label Y`; the
# raw value goes in the `Authorization: Bearer <raw>` header and
# `apps.tokens.middleware.BearerTokenAuthMiddleware` resolves it to a
# real Django user. The legacy shared-secret WORKBENCH_WRITE_TOKEN +
# CANOPY_E2E_AUTH_TOKEN env vars were retired by the PAT refactor.

# --- MCP server (apps/mcp) -------------------------------------------
# FastMCP 3.x Streamable-HTTP server mounted at /api/mcp/. Auth is per-user
# Personal Access Token (apps/tokens) — minting a PAT requires logging in,
# which is exactly the intended access gate, so no separate OAuth flow is
# needed. See apps/mcp/server.py.
# Per-user write rate limit for mutating MCP tools (e.g. clear_insights).
MCP_WRITE_LIMIT = env.int("MCP_WRITE_LIMIT", default=10)
MCP_WRITE_WINDOW_SECONDS = env.int("MCP_WRITE_WINDOW_SECONDS", default=60)

# --- Web Push (VAPID) ---
# The PUBLIC key is not a secret — it ships in the JS bundle; the browser needs
# it to subscribe. The PRIVATE key signs every send and must never leave the
# server (prod: Secrets Manager, see deploy/aws/canopy-web.cfn.yaml).
# Empty keys disable push: the endpoints 503 and nothing is ever sent, so a
# deployment without them degrades rather than 500s.
VAPID_PUBLIC_KEY = env("VAPID_PUBLIC_KEY", default="")
VAPID_PRIVATE_KEY = env("VAPID_PRIVATE_KEY", default="")
# Contact for the push service if our sends misbehave. Must be a mailto: URL.
VAPID_SUBJECT = env("VAPID_SUBJECT", default="mailto:jjackson@dimagi.com")

# AI Backend: "api" (direct Anthropic SDK) or "cli" (claude code CLI)
AI_BACKEND = env("AI_BACKEND", default="api")
ANTHROPIC_API_KEY = env("ANTHROPIC_API_KEY", default="")

# --- Chat execution (apps/canopy_sessions, SP2b) ---
# When True (dev/test default), a chat "send" runs the stub executor INLINE so the
# turn completes with no runner present. When False (production, connectlabs), the
# send just enqueues the session turn and a session-capable cloud runner claims +
# executes it (real claude). The one knob that flips chat from stub to cloud runner.
CHAT_STUB_EXECUTOR = env.bool("CHAT_STUB_EXECUTOR", default=True)

# Key for at-rest field encryption (per-runner credentials — apps/common/encryption.py).
# Empty → derived from SECRET_KEY (fine for dev). Set explicitly in production so the
# encryption key can rotate independently of SECRET_KEY.
FIELD_ENCRYPTION_KEY = env("FIELD_ENCRYPTION_KEY", default="")

# This deployment's own externally-reachable base URL — no request context to derive
# it from when services.py builds a callback URL for a drilled agent to POST back to
# (a shell prompt, not an HTTP view). connectlabs.py overrides to the labs URL.
CANOPY_PUBLIC_BASE_URL = env("CANOPY_PUBLIC_BASE_URL", default="http://localhost:8000")

# CORS
if DEBUG:
    CORS_ALLOW_ALL_ORIGINS = True

# --- Logging ---
# There was no LOGGING config at all, so anything our code logged fell through to
# Python's `lastResort` handler: WARNING+ to stderr, with no timestamp, level or
# logger name. Combined with allauth rendering a failed OAuth callback as HTTP
# 200, that left the container emitting uvicorn access lines and essentially
# nothing else — the 2026-07-26 labs auth incident had to be reconstructed from
# status codes and a repeated query param.
#
# `disable_existing_loggers: False` is load-bearing: uvicorn configures its own
# loggers (including the access log this deployment is read through), and Django
# applies this dictConfig at settings load. Setting it True would silence them.
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {
            "format": "{levelname} {asctime} {name} {message}",
            "style": "{",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "verbose",
        },
    },
    "loggers": {
        # Our own code. WARNING by default; DJANGO_LOG_LEVEL=DEBUG turns the
        # volume up without a code change when something needs watching live.
        "apps": {
            "handlers": ["console"],
            "level": env("DJANGO_LOG_LEVEL", default="WARNING"),
            "propagate": False,
        },
        # allauth's own complaints about the auth cycle, which we otherwise
        # never see (see CustomSocialAccountAdapter.on_authentication_error).
        "allauth": {
            "handlers": ["console"],
            "level": env("DJANGO_LOG_LEVEL", default="WARNING"),
            "propagate": False,
        },
    },
}

