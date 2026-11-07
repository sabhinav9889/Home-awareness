"""Encrypted snapshot storage. Fernet-encrypted JPEGs on disk, one row each.

Never log the key, image bytes, or anything derived from them. Paths are random UUIDs.
"""

import asyncio
import logging
import os
import uuid
from datetime import datetime, timedelta

from cryptography.fernet import Fernet
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.households.models import Household
from app.snapshots.models import Snapshot

log = logging.getLogger(__name__)

MAX_BYTES = 300 * 1024  # larger images are dropped (the event is kept)
CAP_BYTES = 200 * 1024 * 1024  # per household; oldest evicted first
ORPHAN_AFTER = timedelta(days=1)
RETENTION_CHOICES = (1, 7, 30)

_warned_no_key = False


def is_jpeg(data: bytes) -> bool:
    """SOI magic (FF D8 FF) and an EOI marker (FF D9) at the end (zero padding tolerated).
    No decoding: this rejects the wrong format and truncated uploads, nothing more."""
    return data[:3] == b"\xff\xd8\xff" and data.rstrip(b"\x00")[-2:] == b"\xff\xd9"


def _fernet() -> Fernet | None:
    global _warned_no_key
    key = get_settings().snapshot_key
    if not key:
        if not _warned_no_key:
            _warned_no_key = True
            log.warning("SNAPSHOT_KEY is not set: camera snapshots are dropped, events still work")
        return None
    return Fernet(key.encode())


def _abs(path: str) -> str:
    base = os.path.realpath(get_settings().snapshot_dir)
    full = os.path.realpath(os.path.join(base, path))
    if not full.startswith(base + os.sep):  # paths come from our own rows; belt and braces
        raise ValueError("snapshot path escapes the snapshot directory")
    return full


def _write_file(path: str, data: bytes) -> None:
    full = _abs(path)
    os.makedirs(os.path.dirname(full), mode=0o700, exist_ok=True)
    tmp = full + ".part"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    os.replace(tmp, full)


def _remove_file(path: str) -> None:
    try:
        os.remove(_abs(path))
    except FileNotFoundError:
        pass
    except Exception:  # noqa: BLE001  a stuck file is retried by the orphan sweep
        log.warning("could not delete a snapshot file", exc_info=True)


async def save(
    session: AsyncSession, household_id: int, event_id: int, jpeg: bytes
) -> Snapshot | None:
    """Validate, encrypt and store `jpeg`; add (and flush) its row in the caller's transaction.

    Returns None, never raises, when the image is unusable or storage fails: a bad image
    must not fail the event. The file is written before the row; if the caller's commit
    then fails, it must call `discard_file(snapshot)`."""
    if len(jpeg) > MAX_BYTES or not is_jpeg(jpeg):
        return None
    path: str | None = None
    try:
        fernet = _fernet()
        if fernet is None:
            return None
        path = f"{household_id}/{uuid.uuid4().hex}.bin"
        await asyncio.to_thread(_write_file, path, fernet.encrypt(jpeg))
        snap = Snapshot(
            household_id=household_id, event_id=event_id, size_bytes=len(jpeg), path=path
        )
        async with session.begin_nested():  # a failed insert must not poison the event's txn
            session.add(snap)
        await session.flush()
        await enforce_cap(session, household_id)
        return snap
    except Exception:  # noqa: BLE001
        log.warning("could not store a snapshot; the event is kept without it", exc_info=True)
        if path is not None:
            await asyncio.to_thread(_remove_file, path)
        return None


async def discard_file(snapshot: Snapshot) -> None:
    """For a caller whose commit failed after `save`: remove the now-unreferenced file."""
    await asyncio.to_thread(_remove_file, snapshot.path)


def load(snapshot: Snapshot) -> bytes:
    """Decrypted JPEG. Raises if the file is missing or can't be decrypted."""
    fernet = _fernet()
    if fernet is None:
        raise RuntimeError("SNAPSHOT_KEY is not set")
    with open(_abs(snapshot.path), "rb") as f:
        return fernet.decrypt(f.read())


async def try_load(snapshot: Snapshot | None) -> bytes | None:
    """`load` off the event loop; None when there is no usable image (never raises)."""
    if snapshot is None:
        return None
    try:
        return await asyncio.to_thread(load, snapshot)
    except Exception:  # noqa: BLE001  lost file, wrong key: the alert goes out as text
        log.warning("snapshot %s is unreadable", snapshot.id)
        return None


async def delete_snapshot(session: AsyncSession, snapshot: Snapshot) -> None:
    await session.execute(delete(Snapshot).where(Snapshot.id == snapshot.id))
    await asyncio.to_thread(_remove_file, snapshot.path)


async def enforce_cap(session: AsyncSession, household_id: int) -> int:
    """Evict a household's oldest snapshots until it is within CAP_BYTES. Returns the count."""
    total = await session.scalar(
        select(func.coalesce(func.sum(Snapshot.size_bytes), 0)).where(
            Snapshot.household_id == household_id
        )
    )
    evicted = 0
    if total <= CAP_BYTES:
        return 0
    oldest = await session.scalars(
        select(Snapshot)
        .where(Snapshot.household_id == household_id)
        .order_by(Snapshot.created_at, Snapshot.id)
    )
    for snap in list(oldest):
        if total <= CAP_BYTES:
            break
        total -= snap.size_bytes
        await delete_snapshot(session, snap)
        evicted += 1
    return evicted


async def purge_expired(session: AsyncSession, now: datetime) -> int:
    """Delete snapshots older than their household's retention (cross-household sweep).
    Rows and files only; events and alerts are untouched."""
    removed = 0
    for days in RETENTION_CHOICES:
        rows = await session.scalars(
            select(Snapshot)
            .join(Household, Household.id == Snapshot.household_id)
            .where(
                Household.snapshot_retention_days == days,
                Snapshot.created_at < now - timedelta(days=days),
            )
        )
        for snap in list(rows):
            await delete_snapshot(session, snap)
            removed += 1
    return removed


def _old_files(older_than: float) -> list[str]:
    base = get_settings().snapshot_dir
    found: list[str] = []
    if not os.path.isdir(base):
        return found
    for hdir in os.scandir(base):
        if not hdir.is_dir(follow_symlinks=False):
            continue
        for f in os.scandir(hdir.path):
            if f.is_file(follow_symlinks=False) and f.stat().st_mtime < older_than:
                found.append(f"{hdir.name}/{f.name}")
    return found


async def purge_orphans(session: AsyncSession, now: datetime) -> int:
    """Delete files no row refers to and that are over a day old. A deleted household or event
    cascades its rows away in the database, which cannot touch the disk; this is what
    removes their files. The age guard keeps it clear of a file whose row is still in an
    uncommitted transaction."""
    cutoff = (now - ORPHAN_AFTER).timestamp()
    candidates = await asyncio.to_thread(_old_files, cutoff)
    removed = 0
    for i in range(0, len(candidates), 500):
        chunk = candidates[i : i + 500]
        known = set(await session.scalars(select(Snapshot.path).where(Snapshot.path.in_(chunk))))
        for path in chunk:
            if path not in known:
                await asyncio.to_thread(_remove_file, path)
                removed += 1
    return removed
