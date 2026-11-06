"""index events.device_id and alerts.member_id

Revision ID: 0008
Revises: 0007
"""

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("ix_events_device_id", "events", ["device_id"])
    op.create_index("ix_alerts_member_id", "alerts", ["member_id"])


def downgrade() -> None:
    op.drop_index("ix_alerts_member_id", table_name="alerts")
    op.drop_index("ix_events_device_id", table_name="events")
