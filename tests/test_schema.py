from app.alerts.models import Alert
from app.events.models import Event


def _indexed(table) -> set[tuple[str, ...]]:
    return {tuple(c.name for c in ix.columns) for ix in table.indexes}


def test_foreign_keys_used_in_lookups_are_indexed():
    assert ("device_id",) in _indexed(Event.__table__)  # SET NULL on device delete, joins
    assert ("member_id",) in _indexed(Alert.__table__)  # /alerts, member deletes
