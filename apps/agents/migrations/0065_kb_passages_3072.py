"""KB passage vectors at gemini-embedding-001's full 3072 dimensions (was 768).

A vector of one size cannot be cast to another, so any rows are dropped first: they
count as missing and the next `kb_embed` fills them. The HNSW index is rebuilt for
the new column type (halfvec indexes up to 4,000 dimensions).
"""
import pgvector.django.halfvec
from django.db import migrations

_DROP_INDEX = "DROP INDEX IF EXISTS kb_passage_embedding_hnsw"
# A denser graph than pgvector's default (m=16, ef_construction=64): at 3072
# dimensions the default lost neighbours an exact scan finds (GiveWell KB, 312
# questions: 255 vs 275 right answers in the top 50); this build recovers them (273).
_HNSW = ("CREATE INDEX IF NOT EXISTS kb_passage_embedding_hnsw ON agents_kbpassage "
         "USING hnsw (embedding halfvec_cosine_ops) WITH (m = 32, ef_construction = 200)")


def _on_postgres(sql):
    def run(apps, schema_editor):
        if schema_editor.connection.vendor == "postgresql":
            schema_editor.execute(sql)
    return run


def _drop_rows(apps, schema_editor):
    apps.get_model("agents", "KbPassage").objects.all().delete()


class Migration(migrations.Migration):
    dependencies = [
        ("agents", "0064_kb_passages"),
    ]

    operations = [
        migrations.RunPython(_on_postgres(_DROP_INDEX), migrations.RunPython.noop),
        migrations.RunPython(_drop_rows, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="kbpassage",
            name="embedding",
            field=pgvector.django.halfvec.HalfVectorField(dimensions=3072),
        ),
        migrations.RunPython(_on_postgres(_HNSW), _on_postgres(_DROP_INDEX)),
    ]
