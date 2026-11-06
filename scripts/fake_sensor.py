"""Fake sensor for trying the server without hardware.

Pairs with a pairing code (saving the device key to .fake_sensor_key, never printing it),
sends events, then keeps sending a heartbeat every 60 s until Ctrl-C (use --once to exit
after the events). Run with: uv run python scripts/fake_sensor.py --base-url URL --code CODE
"""

import argparse
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx

KEY_FILE = Path(".fake_sensor_key")
HEARTBEAT_S = 60


def save_key(key: str) -> None:
    fd = os.open(KEY_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(key)


def pair(client: httpx.Client, code: str) -> str:
    r = client.post("/device/pair", json={"code": code})
    r.raise_for_status()
    body = r.json()
    save_key(body["device_key"])
    print(f"paired: device_id={body['device_id']} (key saved to {KEY_FILE})")
    return body["device_key"]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--base-url", required=True)
    p.add_argument("--code", help="pairing code; omit to reuse the key in .fake_sensor_key")
    p.add_argument("--sound", default="scream")
    p.add_argument("--count", type=int, default=1)
    p.add_argument("--interval", type=float, default=1, help="seconds between events")
    p.add_argument("--once", action="store_true", help="exit after the events, no heartbeats")
    args = p.parse_args(argv)

    with httpx.Client(base_url=args.base_url, timeout=10) as client:
        if args.code:
            key = pair(client, args.code)
        elif KEY_FILE.exists():
            key = KEY_FILE.read_text().strip()
            print(f"reusing key from {KEY_FILE}")
        else:
            print("need --code (no saved key)", file=sys.stderr)
            return 2
        headers = {"X-Device-Key": key}

        def heartbeat() -> None:
            r = client.post("/device/heartbeat", headers=headers)
            print(f"heartbeat -> {r.status_code}")

        heartbeat()
        for i in range(args.count):
            if i:
                time.sleep(args.interval)
            event = {
                "type": args.sound,
                "confidence": 0.9,
                "occurred_at": datetime.now(UTC).isoformat(),
            }
            r = client.post("/device/events", json=event, headers=headers)
            print(f"event {i + 1}/{args.count} -> {r.status_code} {r.text}")
        if args.once:
            return 0
        print(f"heartbeating every {HEARTBEAT_S}s; Ctrl-C to stop")
        try:
            while True:
                time.sleep(HEARTBEAT_S)
                heartbeat()
        except KeyboardInterrupt:
            print("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
