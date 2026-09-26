"""
Production Django settings for the AWS ECS Fargate deployment
(https://labs.connect.dimagi.com/canopy/; provisioned by deploy/aws/canopy-web.cfn.yaml).
"""
from django.core.exceptions import ImproperlyConfigured

from .base import *  # noqa: F401, F403
from .base import INSECURE_DEV_SECRET_KEY, SECRET_KEY

DEBUG = False
# Never generate throwaway OAuth client keys in a deployment, whatever the env
# says: a host would cache a key that dies with this process.
CANOPY_OAUTH_EPHEMERAL_KEYS = False

# Refuse to boot on the insecure dev default. base.py keeps that default so local
# `runserver` works with no setup, but in production it must come from the
# environment (the SECRET_KEY secret in the ECS task def). This assertion is the
# durable fix for the DJANGO_SECRET_KEY/SECRET_KEY name mismatch that let prod
# silently sign cookies with the public placeholder — a misconfigured deploy now
# crashes loudly instead of running compromised.
if SECRET_KEY == INSECURE_DEV_SECRET_KEY:
    raise ImproperlyConfigured(
        "SECRET_KEY is the insecure dev default in a production settings module. "
        "Set the SECRET_KEY environment variable to a strong secret "
        "(ECS task def maps it from the canopy-web/django-secret-key Secrets Manager entry)."
    )

# Cloud Run sets PORT; uvicorn binds to it.
# ALLOWED_HOSTS was declared as a list type in base.py, so env() returns a list.
ALLOWED_HOSTS = env.list("ALLOWED_HOSTS", default=["*"])

# Security headers — Cloud Run terminates TLS
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
CSRF_COOKIE_SECURE = True
SESSION_COOKIE_SECURE = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SAMESITE = "Lax"

# Static files — served by WhiteNoise in the single Cloud Run service
STATIC_URL = "/static/"
STATICFILES_STORAGE = "whitenoise.storage.CompressedManifestStaticFilesStorage"

# Also serve the built SPA (frontend/dist) at the root via WhiteNoise.
# WHITENOISE_INDEX_FILE makes it return index.html for directory requests
# like '/'. Routes that aren't files (e.g. /projects) fall through to Django's
# spa_view, which also returns index.html so React Router can take over.
WHITENOISE_ROOT = FRONTEND_DIST_DIR  # noqa: F405 (from base.py *)
WHITENOISE_INDEX_FILE = True

# CSRF trusted origins in prod come from ALLOWED_HOSTS
CSRF_TRUSTED_ORIGINS = [f"https://{h}" for h in ALLOWED_HOSTS if h and h != "*"]

# Database — Cloud SQL via Unix socket
# DATABASE_URL format: postgres://USER:PASSWORD@//cloudsql/PROJECT:REGION:INSTANCE/DBNAME
# django-environ handles the //cloudsql/ socket path automatically

# Logging
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "json": {
            "format": '{"severity":"%(levelname)s","message":"%(message)s","module":"%(module)s"}',
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "json",
        },
    },
    "root": {
        "handlers": ["console"],
        "level": "INFO",
    },
}
