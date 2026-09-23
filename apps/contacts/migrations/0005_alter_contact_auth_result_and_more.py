"""Drop `app_signed_origin` from the grade ladder — choices only, no data.

Nothing ever assigned it, so no row can hold it: the only grade an embedded
site's visitor is ever given is `app_signed` (`tokens/contact_api.py`), and a
visitor canopy resolves to an account stops being a contact altogether. Were a
stray value to exist anyway, `AUTH_RANK.get(grade, 0)` reads it as 0, which is
the fail-closed direction — so there is deliberately no data migration here
inventing a value for a row that does not exist.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("contacts", "0004_dkim_aligned_grade"),
    ]

    operations = [
        migrations.AlterField(
            model_name="contact",
            name="auth_result",
            field=models.CharField(
                choices=[
                    ("none", "Unverified"),
                    ("spf", "SPF only (envelope sender)"),
                    ("app_secret", "App credential (proves the app, not the person)"),
                    ("dkim", "DKIM signed"),
                    ("app_signed", "Signed assertion from the app"),
                    ("slack", "Slack account (event signed by Slack)"),
                    ("dmarc", "DMARC aligned"),
                    ("dkim_aligned", "DKIM signed by the From: domain"),
                ],
                default="none",
                help_text="The BEST grade seen from this address so far. Best rather than latest: one forwarded message that breaks SPF should not downgrade a correspondent, and a rule asking 'has this domain ever proved itself' wants the high-water mark.",
                max_length=20,
            ),
        ),
        migrations.AlterField(
            model_name="contact",
            name="last_auth_result",
            field=models.CharField(
                choices=[
                    ("none", "Unverified"),
                    ("spf", "SPF only (envelope sender)"),
                    ("app_secret", "App credential (proves the app, not the person)"),
                    ("dkim", "DKIM signed"),
                    ("app_signed", "Signed assertion from the app"),
                    ("slack", "Slack account (event signed by Slack)"),
                    ("dmarc", "DMARC aligned"),
                    ("dkim_aligned", "DKIM signed by the From: domain"),
                ],
                default="none",
                help_text="The grade on the most recent message. Kept beside the best one because a DROP is the interesting signal — a correspondent who always passed DMARC and suddenly does not is worth noticing.",
                max_length=20,
            ),
        ),
    ]
