"""alert status 'delivering' (at-most-once claim)

Revision ID: 0007
Revises: 0006
"""

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("alerts_status_check", "alerts", type_="check")
    op.create_check_constraint(
        "alerts_status_check",
        "alerts",
        "status IN ('pending','delivering','sent','partial','failed')",
    )


def downgrade() -> None:
    op.execute("UPDATE alerts SET status='failed' WHERE status='delivering'")
    op.drop_constraint("alerts_status_check", "alerts", type_="check")
    op.create_check_constraint(
        "alerts_status_check", "alerts", "status IN ('pending','sent','partial','failed')"
    )
