"""Module-level settings setup that has no other natural test home."""


def test_whitenoise_serves_the_webmanifest_as_manifest_json():
    """The PWA manifest must not go out as application/octet-stream.

    WhiteNoise's MediaTypes map is its own — it never consults Python's
    mimetypes and has no .webmanifest entry. An earlier fix called
    mimetypes.add_type() and shipped a test asserting mimetypes.guess_type():
    green test, prod still served octet-stream. So assert through WhiteNoise's
    own resolution, which is what actually decides the header.
    """
    from django.conf import settings
    from whitenoise.media_types import MediaTypes

    media = MediaTypes(extra_types=settings.WHITENOISE_MIMETYPES)
    assert media.get_type("manifest.webmanifest") == "application/manifest+json"


def _deployed_setting(names, **env):
    """Read settings as the deployment loads them (`config.settings.connectlabs`),
    in a fresh interpreter: the production module refuses the dev SECRET_KEY and
    reads its env at import, so it cannot be imported into the test process."""
    import json
    import os
    import subprocess
    import sys

    code = ("import json, django; from django.conf import settings; django.setup(); "
            f"print(json.dumps({{n: getattr(settings, n) for n in {list(names)!r}}}))")
    full_env = {k: v for k, v in os.environ.items() if not k.startswith("PASSKEY_")}
    full_env.update({"DJANGO_SETTINGS_MODULE": "config.settings.connectlabs",
                     "SECRET_KEY": "a-test-secret-that-is-not-the-dev-default",
                     "DATABASE_URL": "sqlite:///:memory:", **env})
    out = subprocess.run([sys.executable, "-c", code], env=full_env, capture_output=True,
                         text=True, check=True)
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_production_pins_the_passkey_relying_party_to_canopy():
    """Not the request's Host: the deployment runs ALLOWED_HOSTS=["*"]."""
    got = _deployed_setting(["PASSKEY_RP_ID", "PASSKEY_ORIGINS"])
    assert got == {"PASSKEY_RP_ID": "canopy.dimagi.com",
                   "PASSKEY_ORIGINS": ["https://canopy.dimagi.com"]}


def test_the_env_still_overrides_the_pinned_relying_party():
    got = _deployed_setting(["PASSKEY_RP_ID", "PASSKEY_ORIGINS"],
                            PASSKEY_RP_ID="example.org",
                            PASSKEY_ORIGINS="https://example.org,https://www.example.org")
    assert got == {"PASSKEY_RP_ID": "example.org",
                   "PASSKEY_ORIGINS": ["https://example.org", "https://www.example.org"]}
