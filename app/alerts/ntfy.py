import httpx

from app.alerts.errors import DeliveryError


async def publish(http: httpx.AsyncClient, base_url: str, topic: str, text: str) -> None:
    try:
        r = await http.post(
            f"{base_url.rstrip('/')}/{topic}",
            content=text.encode(),
            headers={"Priority": "high"},
        )
    except httpx.HTTPError as e:
        raise DeliveryError(f"ntfy request failed: {type(e).__name__}") from None
    if r.status_code != 200:
        raise DeliveryError(f"ntfy {r.status_code}")
