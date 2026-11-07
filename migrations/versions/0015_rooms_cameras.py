"""rooms, camera devices, capabilities, consent

Revision ID: 0015
Revises: 0014
"""

import sqlalchemy as sa
from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "rooms",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("household_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=60), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("kind IN ('common','entrance','bedroom','bathroom','outdoor','other')"),
        sa.ForeignKeyConstraint(["household_id"], ["households.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("household_id", "name"),
    )
    op.create_index(op.f("ix_rooms_household_id"), "rooms", ["household_id"], unique=False)

    op.add_column("devices", sa.Column("room_id", sa.Integer(), nullable=True))
    op.add_column(
        "devices",
        sa.Column("capabilities", sa.String(length=16), server_default="audio", nullable=False),
    )
    op.add_column(
        "devices",
        sa.Column("send_snapshots", sa.Boolean(), server_default=sa.true(), nullable=False),
    )
    op.add_column(
        "devices", sa.Column("consent_confirmed_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("devices", sa.Column("consent_confirmed_by", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "devices_room_id_fkey", "devices", "rooms", ["room_id"], ["id"], ondelete="SET NULL"
    )
    op.create_foreign_key(
        "devices_consent_confirmed_by_fkey",
        "devices",
        "members",
        ["consent_confirmed_by"],
        ["id"],
        ondelete="SET NULL",
    )
    op.drop_constraint("devices_kind_check", "devices", type_="check")
    op.create_check_constraint(
        "devices_kind_check", "devices", "kind IN ('laptop','phone','pi','camera','other')"
    )

    op.add_column("pairing_codes", sa.Column("room_id", sa.Integer(), nullable=True))
    op.add_column(
        "pairing_codes",
        sa.Column("capabilities", sa.String(length=16), server_default="audio", nullable=False),
    )
    op.add_column(
        "pairing_codes",
        sa.Column("consent_confirmed", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    op.create_foreign_key(
        "pairing_codes_room_id_fkey",
        "pairing_codes",
        "rooms",
        ["room_id"],
        ["id"],
        ondelete="SET NULL",
    )


# The catalog before this branch (listed here so the migration never depends on app code).
_PRE_BRANCH_SOUND_KEYS = (
    "scream",
    "shouting",
    "crying",
    "moaning",
    "gasping",
    "coughing",
    "glass_breaking",
    "door_slam",
    "knocking",
    "car_alarm",
    "dog_barking",
    "smoke_alarm",
    "siren",
    "explosion",
    "running_water",
    "thud_fall",
    "doorbell",
)


def downgrade() -> None:
    # Events of sounds the old catalog does not know would break the old code reading them.
    keys = ", ".join(f"'{k}'" for k in _PRE_BRANCH_SOUND_KEYS)
    op.execute(f"DELETE FROM events WHERE sound_key NOT IN ({keys})")
    op.drop_constraint("pairing_codes_room_id_fkey", "pairing_codes", type_="foreignkey")
    op.drop_column("pairing_codes", "consent_confirmed")
    op.drop_column("pairing_codes", "capabilities")
    op.drop_column("pairing_codes", "room_id")

    op.execute("DELETE FROM devices WHERE kind='camera'")
    op.drop_constraint("devices_kind_check", "devices", type_="check")
    op.create_check_constraint(
        "devices_kind_check", "devices", "kind IN ('laptop','phone','pi','other')"
    )
    op.drop_constraint("devices_consent_confirmed_by_fkey", "devices", type_="foreignkey")
    op.drop_constraint("devices_room_id_fkey", "devices", type_="foreignkey")
    op.drop_column("devices", "consent_confirmed_by")
    op.drop_column("devices", "consent_confirmed_at")
    op.drop_column("devices", "send_snapshots")
    op.drop_column("devices", "capabilities")
    op.drop_column("devices", "room_id")
    op.drop_index(op.f("ix_rooms_household_id"), table_name="rooms")
    op.drop_table("rooms")
