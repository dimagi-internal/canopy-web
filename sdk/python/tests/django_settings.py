"""Minimal Django settings for the SDK's own suite (pytest-django)."""
from pathlib import Path

SECRET_KEY = "sdk-tests-not-secret"
DEBUG = False
ALLOWED_HOSTS = ["*"]
USE_TZ = True
INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "canopy_sdk.django",
]
MIDDLEWARE = [
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "canopy_sdk.django.middleware.CanopyPageTokenMiddleware",
]
ROOT_URLCONF = "tests.urls"
DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}}
CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
TEMPLATES = [{
    "BACKEND": "django.template.backends.django.DjangoTemplates",
    "DIRS": [Path(__file__).parent / "templates"],
    "APP_DIRS": True,
    "OPTIONS": {"context_processors": ["django.template.context_processors.request",
                                       "django.template.context_processors.csrf"]},
}]
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
CANOPY_HOST = {}
