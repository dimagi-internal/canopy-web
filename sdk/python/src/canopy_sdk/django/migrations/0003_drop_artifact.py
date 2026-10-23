from django.db import migrations


class Migration(migrations.Migration):
    """Kept results moved out of the SDK (0.19): a host keeps its own MCP results.
    Its rows were 24-hour results, so nothing is carried over."""

    dependencies = [("canopy_host", "0002_artifact")]

    operations = [migrations.DeleteModel(name="Artifact")]
