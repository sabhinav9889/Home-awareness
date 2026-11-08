"""Fake camera for trying camera alerts without a real camera.

Pairs with a camera pairing code (made on the Sensors page with "Add a camera"), then sends one
event (default: a fall) with a JPEG snapshot, the way the real camera client will.
The device key is saved to .fake_camera_key (never printed).

First time:  uv run python scripts/fake_camera.py --base-url http://localhost:8000 \
                 --code CODE --image /tmp/test.jpg
Later:       uv run python scripts/fake_camera.py --base-url http://localhost:8000 \
                 --image /tmp/test.jpg --sound fall

Make a test image:  ffmpeg -f lavfi -i testsrc=size=640x360 -frames:v 1 /tmp/test.jpg
"""

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

import httpx

KEY_FILE = Path(".fake_camera_key")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--base-url", required=True)
    p.add_argument("--code", help="camera pairing code; omit to reuse the saved key")
    p.add_argument("--image", required=True, help="a JPEG, 300 KB or smaller")
    p.add_argument("--sound", default="fall", help="fall, lying_still, person_away, weapon_seen")
    p.add_argument("--confidence", type=float, default=0.9)
    args = p.parse_args(argv)

    image = Path(args.image).read_bytes()
    with httpx.Client(base_url=args.base_url, timeout=10) as client:
        if args.code:
            r = client.post("/device/pair", json={"code": args.code})
            r.raise_for_status()
            body = r.json()
            fd = os.open(KEY_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(body["device_key"])
            key = body["device_key"]
            print(f"paired: device_id={body['device_id']} (key saved to {KEY_FILE})")
        elif KEY_FILE.exists():
            key = KEY_FILE.read_text().strip()
        else:
            print("need --code (no saved key)", file=sys.stderr)
            return 2
        headers = {"X-Device-Key": key}

        r = client.post("/device/heartbeat", headers=headers)
        print(f"heartbeat -> {r.status_code} (guarding: {r.json().get('guarding')})")

        event = {
            "type": args.sound,
            "confidence": args.confidence,
            "occurred_at": datetime.now(UTC).isoformat(),
        }
        r = client.post(
            "/device/events",
            headers=headers,
            files={
                "event": (None, json.dumps(event), "application/json"),
                "snapshot": ("snapshot.jpg", image, "image/jpeg"),
            },
        )
        print(f"{args.sound} event -> {r.status_code} {r.text}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
