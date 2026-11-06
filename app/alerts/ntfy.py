from typing import Literal

import httpx

from app.alerts.errors import DeliveryError


async def publish(
    http: httpx.AsyncClient,
    base_url: str,
    topic: str,
    text: str,
    *,
    priority: Literal["urgent", "high"] = "high",
    tags: str | None = None,
) -> None:
    headers = {"Priority": priority}
    if tags:
        headers["Tags"] = tags
    try:
        r = await http.post(
            f"{base_url.rstrip('/')}/{topic}",
            content=text.encode(),
            headers=headers,
        )
    except httpx.HTTPError as e:
        raise DeliveryError(f"ntfy request failed: {type(e).__name__}") from None
    if r.status_code != 200:
        raise DeliveryError(f"ntfy {r.status_code}")
