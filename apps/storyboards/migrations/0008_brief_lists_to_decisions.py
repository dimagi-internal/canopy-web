"""Brief lists (#1501) -> StoryboardDecision rows.

Idempotent: a board whose brief no longer carries the lists is skipped, so a
re-run changes nothing. Uses historical models; the conversion helpers are
pure functions of the brief dict.
"""

from django.db import migrations

from apps.storyboards.decisions import LEGACY_BRIEF_KEYS, from_legacy_brief, validate


def forwards(apps, schema_editor):
    Storyboard = apps.get_model("storyboards", "Storyboard")
    Decision = apps.get_model("storyboards", "StoryboardDecision")
    for board in Storyboard.objects.all():
        if not isinstance(board.brief, dict):
            continue
        brief = dict(board.brief)
        if not any(brief.get(k) for k in LEGACY_BRIEF_KEYS):
            continue
        taken = set(Decision.objects.filter(storyboard=board).values_list("key", flat=True))
        start = len(taken)
        for pos, row in enumerate(from_legacy_brief(brief, taken=taken)):
            clean = validate(row)
            key = clean.pop("id")
            Decision.objects.create(storyboard=board, key=key, position=start + pos, **clean)
        board.brief = {k: v for k, v in brief.items() if k not in LEGACY_BRIEF_KEYS}
        board.save(update_fields=["brief"])


class Migration(migrations.Migration):
    dependencies = [("storyboards", "0007_storyboarddecision")]
    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
