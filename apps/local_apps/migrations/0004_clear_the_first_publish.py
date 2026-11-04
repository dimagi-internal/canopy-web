"""Clears the first published versions, from when the transcript source was its own app.

A person now installs one app, and old manifests name `consumes` and `provides`, which
nothing reads any more. They were up for one day, with nobody relying on them, and
publishing the merged app replaces them. Installs are redone from the store."""
from django.db import migrations


def clear(apps, schema_editor):
    apps.get_model("local_apps", "LocalAppRun").objects.all().delete()
    apps.get_model("local_apps", "LocalAppInstallation").objects.all().delete()
    apps.get_model("local_apps", "LocalAppSubmission").objects.all().delete()
    templates = apps.get_model("workspace_apps", "AppTemplate").objects.filter(runtime="local")
    apps.get_model("workspace_apps", "AppTemplateVersion").objects.filter(template__in=templates).delete()
    templates.delete()


class Migration(migrations.Migration):
    dependencies = [("local_apps", "0003_installation_belongs_to_the_person"), ("workspace_apps", "0004_apptemplate_runtime")]
    operations = [migrations.RunPython(clear, migrations.RunPython.noop)]
