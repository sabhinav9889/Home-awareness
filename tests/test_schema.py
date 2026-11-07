from app.alerts.models import Alert
from app.events.models import Event


def _indexed(table) -> set[tuple[str, ...]]:
    return {tuple(c.name for c in ix.columns) for ix in table.indexes}


def test_foreign_keys_used_in_lookups_are_indexed():
    assert ("device_id",) in _indexed(Event.__table__)  # SET NULL on device delete, joins
    assert ("member_id",) in _indexed(Alert.__table__)  # /alerts, member deletes


def test_stale_sweep_has_a_partial_index_on_unfinished_alerts():
    ix = next(i for i in Alert.__table__.indexes if i.name == "ix_alerts_unfinished_created_at")
    assert [c.name for c in ix.columns] == ["created_at"]
    where = str(ix.dialect_options["postgresql"]["where"])
    assert "pending" in where and "delivering" in where


async def test_partial_index_exists_in_the_database(session):
    from sqlalchemy import text

    indexdef = await session.scalar(
        text("SELECT indexdef FROM pg_indexes WHERE indexname = 'ix_alerts_unfinished_created_at'")
    )
    assert indexdef is not None and "WHERE" in indexdef and "created_at" in indexdef
