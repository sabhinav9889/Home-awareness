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
