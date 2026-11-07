"""partial index on alerts(created_at) for unfinished alerts (stale sweep)

Revision ID: 0013
Revises: 0012
"""

import sqlalchemy as sa
from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_alerts_unfinished_created_at",
        "alerts",
        ["created_at"],
        postgresql_where=sa.text("status IN ('pending', 'delivering')"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_alerts_unfinished_created_at",
        table_name="alerts",
        postgresql_where=sa.text("status IN ('pending', 'delivering')"),
    )
