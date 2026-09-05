"""Actor-aware runner routing (spec 2026-09-05-actor-aware-runner-routing-design).

Schema-only and backwards compatible in both directions for one deploy:

  - `actor` is added blank-defaulted, so every existing row keeps meaning exactly
    what it means today (a rule that applies to any actor), and code running the
    OLD composition simply never reads the column.
  - The constraint swap RELAXES: `one_priority_runner_per_agent_source` capped a
    rule at one runner; the replacement caps a RUNNER at one row per rule. A
    relaxation cannot fail on existing data, and no row has a non-empty `actor`
    yet, so the new constraint is trivially satisfied on apply.

No data migration. `rank` — previously written 0 and ignored on a source row —
becomes meaningful WITHIN a rule, and 0 stays correct for a rule of length one.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('agents', '0017_delete_agentturn'),
        ('harness', '0039_turn_attempts'),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name='runnerassignment',
            name='one_priority_runner_per_agent_source',
        ),
        migrations.AddField(
            model_name='runnerassignment',
            name='actor',
            field=models.CharField(blank=True, default='', max_length=254),
        ),
        migrations.AddConstraint(
            model_name='runnerassignment',
            constraint=models.UniqueConstraint(
                condition=models.Q(('source', ''), _negated=True),
                fields=('agent', 'source', 'actor', 'runner'),
                name='one_row_per_runner_per_agent_source_actor',
            ),
        ),
    ]
