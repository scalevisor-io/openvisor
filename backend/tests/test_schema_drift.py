"""The models and the migrations must describe the SAME schema.

Drift in either direction is dangerous and both have bitten us:
- DB ahead of the models (an index created only in a migration): every later
  `alembic revision --autogenerate` proposes DROPPING it, so a distracted merge
  ships an unintended drop - `ix_credit_transaction_created_at` sat like that
  from 2026-08-07 to 2026-08-10.
- Models ahead of the DB (a column added without its migration): prod boots
  against a table that lacks it and the queries fail.

This is the automatic version of "diff the model against a database migrated to
head" - it runs against the test DB, which the entrypoint has upgraded to head.
"""
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext

from app.core.db import sync_engine
from app.models.models import Base


def test_models_and_migrations_agree():
    with sync_engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    assert diff == [], (
        "The models and the migrated database disagree:\n  "
        + "\n  ".join(repr(d) for d in diff)
        + "\n\nAdd the missing declaration to models.py (when the database is right - "
          "e.g. an index a migration created but no column declares), or generate the "
          "missing migration with `make makemigration M=\"...\"` (when the model is right). "
          "Never let autogenerate's proposed DROP ride along in an unrelated migration."
    )
