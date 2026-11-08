"""Console entry point: ``ha-camera``. The device key is never printed or logged."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
from collections.abc import Sequence

from ha_edge.cli import (
    EXIT_ERROR,
    EXIT_OK,
    EXIT_USAGE,
    _err,
    _parser,
)

NOTICE = (
    "This camera is watching: frames are analysed on this computer and never saved or sent; "
    "snapshots only on alerts (one still photo per alert, if your household allows it)."
)


def _cmd_pair(args: argparse.Namespace) -> int:
    from ha_edge import api, config

    async def pair() -> tuple[int, str]:
        client = api.ServerClient(args.server)
        try:
            return await client.pair(args.code)
        finally:
            await client.aclose()

    try:
        device_id, key = asyncio.run(pair())
    except api.PairingFailed as e:
        _err(f"Pairing failed: {e}. Create a new pairing code and try again.")
        return EXIT_USAGE
    except api.RateLimited as e:
        _err(f"Pairing failed: Too many attempts — wait {e.retry_after_s:g} s and try again.")
        return EXIT_USAGE
    except api.Rejected as e:
        if e.status == 404:
            _err(
                f"Pairing failed: {args.server} answered 404 Not Found — check the server URL "
                "(the address of your Home Awareness server, e.g. https://example.org)."
            )
        else:
            _err(f"Pairing failed: the server refused the request ({e}).")
        return EXIT_USAGE
    except api.ApiError as e:
        _err(f"Pairing failed: cannot reach the server at {args.server} ({e}).")
        return EXIT_ERROR
    name = args.camera
    index = None
    with contextlib.suppress(config.ConfigError):
        old = config.load_camera()
        if old is not None:
            index = old.camera_index
            if name is None:
                name = old.camera_name
            elif name != old.camera_name:
                index = None  # a different camera was asked for: forget the old node
    config.save_camera(config.CameraConfig(args.server, device_id, key, name, index))
    print(f"Paired as device {device_id}.")
    print(NOTICE)
    return EXIT_OK


def _cmd_cameras(args: argparse.Namespace) -> int:
    from ha_edge.camera import devices

    cams = devices.list_cameras()
    if not cams:
        print("No cameras found.")
        return EXIT_OK
    width = max(len(c.name) for c in cams)
    print(f"  {'INDEX':>5}  {'NAME':<{width}}  NODE")
    for c in cams:
        print(f"  {c.index:>5}  {c.name:<{width}}  {c.node}")
    return EXIT_OK


def _cmd_fetch_model(args: argparse.Namespace) -> int:
    import httpx

    from ha_edge.camera import models

    try:
        dest = models.fetch_models(models.default_model_dir())
    except (OSError, httpx.HTTPError, models.ModelIntegrityError) as e:
        _err(f"Cannot download the camera models: {e}")
        return EXIT_ERROR
    print(dest / models.POSE_FILE)
    print(dest / models.OBJECT_FILE)
    return EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser("ha-camera", "Turn a camera into a Home Awareness sensor.")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("pair", help="pair this camera with your Home Awareness account")
    p.add_argument("--server", required=True, help="server URL, e.g. https://example.org")
    p.add_argument("--code", required=True, help="pairing code from the web app")
    p.add_argument("--camera", help="camera name (or unique part of it); saved for next time")
    sub.add_parser("cameras", help="list cameras (without opening them)")
    sub.add_parser("fetch-model", help="download and verify the pose and object models")
    args = parser.parse_args(argv)
    commands = {"pair": _cmd_pair, "cameras": _cmd_cameras, "fetch-model": _cmd_fetch_model}
    try:
        return commands[args.command](args)
    except KeyboardInterrupt:
        return EXIT_OK
