"""household Away mode, night hours, snapshot retention

Revision ID: 0016
Revises: 0015
"""

import sqlalchemy as sa
from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "households",
        sa.Column("away", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.add_column("households", sa.Column("night_start", sa.Time(), nullable=True))
    op.add_column("households", sa.Column("night_end", sa.Time(), nullable=True))
    op.add_column(
        "households",
        sa.Column(
            "snapshot_retention_days", sa.Integer(), server_default=sa.text("7"), nullable=False
        ),
    )
    op.create_check_constraint(
        "ck_households_retention", "households", "snapshot_retention_days IN (1, 7, 30)"
    )


def downgrade() -> None:
    op.drop_constraint("ck_households_retention", "households", type_="check")
    op.drop_column("households", "snapshot_retention_days")
    op.drop_column("households", "night_end")
    op.drop_column("households", "night_start")
    op.drop_column("households", "away")
