"""The dedicated user canopy's live probe acts as at canopy's OWN MCP.

canopy-web is a host of its own MCP (`apps/tokens/self_host.py`), so its
`canopy-web` Connected site is probed like any other (`apps/tokens/live_probe.py`):
its probe endpoint issues a real ID-JAG for ONE fixed principal, and this is it.

Deliberately nobody: not staff, not a superuser, no usable password, no email
(so no Google sign-in can ever land on it), and no workspace membership — the
probe's `list_insights` call runs as a real, active account and reads nothing.

Created only where a deployment names it (`CANOPY_HOST_PROBE_USERNAME`, set in
`connectlabs.py`), so dev and test databases never grow an extra user: several
things pick "the first user" (`ensure_default_workspace`, which also skips this
one by name). A deployment that turns the probe on later can re-run this with
`manage.py migrate tokens 0025 && manage.py migrate tokens`.
"""
from django.conf import settings
from django.contrib.auth.hashers import make_password
from django.db import migrations


def _username() -> str:
    return (getattr(settings, "CANOPY_HOST_PROBE_USERNAME", "") or "").strip()


def create(apps, schema_editor):
    username = _username()
    if not username:
        return
    User = apps.get_model(*settings.AUTH_USER_MODEL.split("."))
    if User.objects.filter(username=username).exists():
        return
    User.objects.create(
        username=username, email="", first_name="canopy", last_name="probe",
        is_active=True, is_staff=False, is_superuser=False,
        # make_password(None) is Django's "unusable password" marker.
        password=make_password(None),
    )


def remove(apps, schema_editor):
    username = _username()
    if not username:
        return
    User = apps.get_model(*settings.AUTH_USER_MODEL.split("."))
    User.objects.filter(username=username, is_staff=False, is_superuser=False).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("tokens", "0025_appcredential_live_probe"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [migrations.RunPython(create, remove)]
