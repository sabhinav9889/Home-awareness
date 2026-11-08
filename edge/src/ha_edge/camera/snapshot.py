"""In-memory JPEG snapshots. Never touches disk."""

from __future__ import annotations

from ha_edge.camera.capture import Frame

_QUALITIES = (85, 75, 65, 55, 45, 40)


def encode_snapshot(
    frame: Frame,
    now: float,
    *,
    max_age_s: float = 1.0,
    max_side: int = 640,
    max_bytes: int = 300_000,
) -> bytes | None:
    """JPEG bytes of the frame, or None if it is stale or cannot fit max_bytes."""
    if now - frame.captured_at > max_age_s:
        return None
    import cv2
    import numpy as np

    if max_side <= 0:
        return None
    try:
        img = np.asarray(frame.image)
        if img.ndim not in (2, 3) or img.size == 0 or 0 in img.shape:
            return None
        if img.ndim == 3:
            if img.shape[2] == 1:
                img = img[:, :, 0]
            elif img.shape[2] == 4:
                img = img[:, :, :3]
            elif img.shape[2] != 3:
                return None
        img = np.ascontiguousarray(img)
        h, w = img.shape[:2]
        scale = max_side / max(h, w)
        if scale < 1:
            size = (max(1, round(w * scale)), max(1, round(h * scale)))
            img = cv2.resize(img, size, interpolation=cv2.INTER_AREA)
        for quality in _QUALITIES:
            ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, quality])
            if not ok:
                return None
            data = buf.tobytes()
            if len(data) <= max_bytes:
                return data
    except (cv2.error, ValueError, TypeError):
        return None
    return None
