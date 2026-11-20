"""Postgres only: two pay periods of one profile can never overlap.

SQLite (local development and the browser tests) cannot enforce an exclusion constraint, so
the database step is skipped there; the application validates the same rule.
"""

import django.contrib.postgres.constraints
import django.contrib.postgres.fields.ranges
from django.db import migrations, models

CONSTRAINT = django.contrib.postgres.constraints.ExclusionConstraint(
    expressions=[
        (
            models.Func(
                models.F("start_date"),
                models.F("end_date"),
                models.Value("[]"),
                function="daterange",
                output_field=django.contrib.postgres.fields.ranges.DateRangeField(),
            ),
            "&&",
        ),
        ("household", "="),
    ],
    name="no_overlapping_pay_periods",
    deferrable=models.Deferrable.DEFERRED,
)


def add_exclusion(apps, schema_editor):
    """Postgres only: SQLite cannot enforce an exclusion constraint, so it is skipped."""
    if schema_editor.connection.vendor != "postgresql":
        return
    # btree_gist lets the household (an integer) share a GiST index with the date range.
    schema_editor.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")
    schema_editor.add_constraint(apps.get_model("budget", "PayPeriod"), CONSTRAINT)


def remove_exclusion(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.remove_constraint(apps.get_model("budget", "PayPeriod"), CONSTRAINT)


class Migration(migrations.Migration):
    dependencies = [("budget", "0001_initial")]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.AddConstraint(model_name="payperiod", constraint=CONSTRAINT)
            ],
            database_operations=[migrations.RunPython(add_exclusion, remove_exclusion)],
        )
    ]
