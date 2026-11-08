"""List cameras from sysfs. Never opens a device node."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

SYSFS_VIDEO4LINUX = Path("/sys/class/video4linux")


@dataclass(frozen=True)
class CameraInfo:
    index: int
    name: str
    node: str


def list_cameras(sysfs: Path = SYSFS_VIDEO4LINUX) -> list[CameraInfo]:
    """Capture nodes (``index`` file == 0); metadata nodes of the same camera are skipped.

    ``CameraInfo.index`` is the N of ``/dev/videoN``.
    """
    sysfs = Path(sysfs)
    try:
        entries = list(sysfs.iterdir())
    except OSError:
        return []
    found: list[CameraInfo] = []
    for entry in entries:
        m = re.fullmatch(r"video(\d+)", entry.name)
        if m is None:
            continue
        try:
            name = (entry / "name").read_text().strip()
            sub_index = int((entry / "index").read_text().strip())
        except (OSError, ValueError):
            continue
        if sub_index != 0:
            continue
        found.append(CameraInfo(int(m.group(1)), name, f"/dev/video{m.group(1)}"))
    return sorted(found, key=lambda c: c.index)
