import base64
from typing import Literal

import httpx

from app.alerts.errors import DeliveryError


def _header(value: str) -> str:
    """Headers are ASCII: anything else goes as an RFC 2047 encoded word (ntfy decodes it)."""
    if value.isascii():
        return value
    return f"=?UTF-8?B?{base64.b64encode(value.encode()).decode()}?="


async def publish(
    http: httpx.AsyncClient,
    base_url: str,
    topic: str,
    text: str,
    *,
    priority: Literal["urgent", "high"] = "high",
    tags: str | None = None,
    attachment: bytes | None = None,
) -> None:
    """A text notification, or with `attachment` (a JPEG) a PUT upload whose Message header
    carries the text. If ntfy answers the upload with a 4xx (too large, refused), the plain
    text is sent instead; a transport error does not fall back (nothing proves it failed)."""
    headers = {"Priority": priority}
    if tags:
        headers["Tags"] = tags
    url = f"{base_url.rstrip('/')}/{topic}"
    try:
        r = None
        if attachment is not None:
            r = await http.put(
                url,
                content=attachment,
                headers={**headers, "Filename": "snapshot.jpg", "Message": _header(text)},
            )
        if r is None or 400 <= r.status_code < 500:
            r = await http.post(url, content=text.encode(), headers=headers)
    except httpx.HTTPError as e:
        raise DeliveryError(f"ntfy request failed: {type(e).__name__}") from None
    if r.status_code != 200:
        raise DeliveryError(f"ntfy {r.status_code}")
