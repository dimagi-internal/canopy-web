from django.apps import AppConfig


class CanopyHostConfig(AppConfig):
    name = "canopy_sdk.django"
    # A label that cannot collide with a host's own apps — canopy-web included,
    # which will mount this beside its own `tokens` app when it acts as a host.
    label = "canopy_host"
    verbose_name = "canopy host grants"
    default_auto_field = "django.db.models.BigAutoField"
