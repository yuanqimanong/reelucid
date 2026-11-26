"""Keep event cursor IDs monotonic when completed jobs are physically deleted."""

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table(
        "events", recreate="always", table_kwargs={"sqlite_autoincrement": True}
    ):
        pass


def downgrade() -> None:
    with op.batch_alter_table(
        "events", recreate="always", table_kwargs={"sqlite_autoincrement": False}
    ):
        pass
