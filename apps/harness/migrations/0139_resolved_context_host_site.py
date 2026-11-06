"""The profile kind `connected_site` is now `host_site`: rewrite the turns already snapshotted, and the
trace and refusal words they hold, so an old turn reads like a new one."""
import json

from django.db import migrations
from django.db.models import TextField
from django.db.models.functions import Cast

_WORDS = (('"connected_site"', '"host_site"'), ("connected_site", "host_site"),
          ("Connected sites", "Host sites"), ("connected sites", "host sites"),
          ("Connected site", "Host site"), ("connected site", "host site"))


def forwards(apps, schema_editor):
    Turn = apps.get_model("harness", "Turn")
    rows = (Turn.objects.exclude(resolved_context=None)
            .annotate(text=Cast("resolved_context", TextField()))
            .filter(text__icontains="connected"))
    for turn in rows.iterator(chunk_size=500):
        text = json.dumps(turn.resolved_context)
        for old, new in _WORDS:
            text = text.replace(old, new)
        Turn.objects.filter(pk=turn.pk).update(resolved_context=json.loads(text))


class Migration(migrations.Migration):
    dependencies = [("harness", "0138_turn_resolved_context")]
    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
