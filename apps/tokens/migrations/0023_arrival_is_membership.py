"""A site's visitor arrives as themselves when they are a member — no domain list.

`resolvable_domains` was a per-site opt-in, empty by default, stacked on the
membership check that actually decides it; so every member arrived as a
contact until an owner found the setting. See `contacts.services.resolve_arrival`.
Nothing is carried over: membership now answers what the list used to.
"""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("tokens", "0022_site_has_no_secret"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="appcredential",
            name="resolvable_domains",
        ),
    ]
