from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("canopy_sessions", "0069_record_retired_project_session_shares"),
    ]
    operations = [migrations.DeleteModel(name="ProjectSessionShare")]
