"""A profile no longer carries `pages`, and each connected site has one entry.

`pages` (a page's declared resource selecting a profile) and the rule that combined the ceilings and
logins of several entries naming one site are gone from the code. Neither was in use: no entry selects
by page, and no site is named by two entries. This migration proves that on the data it runs against
before it changes anything, and refuses to run otherwise, so that removing them cannot change anyone's
access silently: a document it refuses keeps working on the previous image, and needs a person to
decide what it should become.

When the data fits, it removes the empty `pages` key every entry an older save wrote still carries.
Nothing else in a document changes.
"""
from django.db import migrations


def problems_of(slug: str, caps: dict) -> list[str]:
    out = []
    by_site: dict[str, list[str]] = {}
    for name, cap in caps.items():
        cap = cap or {}
        if cap.get("pages"):
            out.append(f"{slug}.{name} selects by page ({cap['pages']})")
        for site in cap.get("sites") or []:
            by_site.setdefault(site, []).append(name)
    out += [f"{slug}: {site} is named by {', '.join(sorted(names))}"
            for site, names in sorted(by_site.items()) if len(names) > 1]
    return out


def forwards(apps, schema_editor):
    Agent = apps.get_model("agents", "Agent")
    agents = list(Agent.objects.order_by("pk").only("pk", "slug", "interface"))
    problems = [p for a in agents for p in problems_of(a.slug, (a.interface or {}).get("capabilities") or {})]
    if problems:
        raise RuntimeError("agents/0132 refuses to run: removing `pages` and the combined site ceiling would "
                           "change what these agents do — " + "; ".join(problems))
    for agent in agents:
        caps = (agent.interface or {}).get("capabilities") or {}
        if not any("pages" in (cap or {}) for cap in caps.values()):
            continue
        for cap in caps.values():
            (cap or {}).pop("pages", None)
        Agent.objects.filter(pk=agent.pk).update(interface=agent.interface)


class Migration(migrations.Migration):
    dependencies = [
        ("agents", "0131_read_mode_enters_through_answer_caller"),
    ]
    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
