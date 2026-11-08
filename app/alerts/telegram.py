import httpx

from app.alerts.errors import DeliveryError


async def send_message(http: httpx.AsyncClient, token: str, chat_id: str, text: str) -> None:
    try:
        r = await http.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text},
        )
    except httpx.HTTPError as e:  # str(e) may embed the URL, which contains the token
        raise DeliveryError(f"telegram request failed: {type(e).__name__}") from None
    try:
        data = r.json()
    except ValueError:
        data = {}
    if r.status_code != 200 or not data.get("ok"):
        raise DeliveryError(f"telegram {r.status_code}: {data.get('description', 'error')}")


class PhotoRefused(DeliveryError):
    """Telegram answered and said no to the photo (a 4xx, or a 200 with ok: false). Only this
    falls back to plain text: a timeout or connection error proves nothing about delivery, so the
    caller's normal retry applies instead (a text fallback could duplicate the alert)."""


async def send_photo(
    http: httpx.AsyncClient, token: str, chat_id: str, jpeg: bytes, caption: str
) -> None:
    try:
        r = await http.post(
            f"https://api.telegram.org/bot{token}/sendPhoto",
            data={"chat_id": chat_id, "caption": caption},
            files={"photo": ("snapshot.jpg", jpeg, "image/jpeg")},
        )
    except httpx.HTTPError as e:
        raise DeliveryError(f"telegram request failed: {type(e).__name__}") from None
    try:
        data = r.json()
    except ValueError:
        data = {}
    if r.status_code != 200 or not data.get("ok"):
        error = f"telegram {r.status_code}: {data.get('description', 'error')}"
        if 400 <= r.status_code < 500 or (r.status_code == 200 and data.get("ok") is False):
            raise PhotoRefused(error)
        raise DeliveryError(error)


async def send_photo_or_message(
    http: httpx.AsyncClient, token: str, chat_id: str, jpeg: bytes, text: str
) -> None:
    """The alert with its photo; if Telegram refuses the photo, the plain text goes instead."""
    try:
        await send_photo(http, token, chat_id, jpeg, text)
    except PhotoRefused:
        await send_message(http, token, chat_id, text)
