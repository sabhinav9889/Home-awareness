"""sound_settings.min_confidence (nullable per-member threshold override)

Revision ID: 0010
Revises: 0009
"""

import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("sound_settings", sa.Column("min_confidence", sa.Float(), nullable=True))
    op.create_check_constraint(
        "ck_sound_settings_min_confidence",
        "sound_settings",
        "min_confidence BETWEEN 0.05 AND 0.95",
    )


def downgrade() -> None:
    op.drop_constraint("ck_sound_settings_min_confidence", "sound_settings", type_="check")
    op.drop_column("sound_settings", "min_confidence")
