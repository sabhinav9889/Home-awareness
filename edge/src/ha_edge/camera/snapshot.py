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

    img = frame.image
    h, w = img.shape[:2]
    scale = max_side / max(h, w)
    if scale < 1:
        img = cv2.resize(img, (round(w * scale), round(h * scale)), interpolation=cv2.INTER_AREA)
    for quality in _QUALITIES:
        ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, quality])
        if not ok:
            return None
        data = buf.tobytes()
        if len(data) <= max_bytes:
            return data
    return None
