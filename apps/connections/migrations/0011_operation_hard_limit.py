import django.utils.timezone
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("connections", "0010_operations_and_surface_shares"),
    ]

    operations = [
        migrations.AddField(
            model_name="connectionoperation",
            name="hard_limit_at",
            # A lease open now simply ends at once: another click opens a new one.
            field=models.DateTimeField(default=django.utils.timezone.now),
            preserve_default=False,
        ),
    ]
