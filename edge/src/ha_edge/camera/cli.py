"""Console entry point: ``ha-camera``. The device key is never printed or logged."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import signal
from collections.abc import Sequence

from ha_edge.cli import (
    EXIT_ERROR,
    EXIT_OK,
    EXIT_USAGE,
    _err,
    _parser,
    _positive_seconds,
    _setup_logging,
)

EXIT_NOT_PAIRED = 3

NOTICE = (
    "This camera is watching: frames are analysed on this computer and never saved or sent; "
    "snapshots only on alerts (one still photo per alert, if your household allows it)."
)


def _match_camera(cams, wanted: str):
    """Cameras matching ``wanted``: an exact name first, else a case-insensitive substring."""
    exact = [c for c in cams if c.name == wanted]
    return exact or [c for c in cams if wanted.lower() in c.name.lower()]


def _cmd_pair(args: argparse.Namespace) -> int:
    from ha_edge import api, config
    from ha_edge.camera import devices

    chosen = None
    if args.camera is not None:
        # Resolve before using up the (single-use) pairing code; save the full name.
        cams = devices.list_cameras()
        if cams:
            matches = _match_camera(cams, args.camera)
            if len(matches) != 1:
                why = "matches more than one camera" if matches else "not found"
                _err(
                    f"Camera {args.camera!r} {why}. Available: "
                    f"{', '.join(c.name for c in cams)}. Use `--camera NAME` to choose one."
                )
                return EXIT_ERROR
            chosen = matches[0]

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
    name = chosen.name if chosen is not None else args.camera
    index = chosen.index if chosen is not None else None
    if args.camera is None:
        with contextlib.suppress(config.ConfigError):
            old = config.load_camera()
            if old is not None:
                name, index = old.camera_name, old.camera_index
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


WATCHING = "Watching — video is analysed on this computer; only a snapshot goes with an alert."


def _pick_camera(flag: str | None, cfg):
    """(CameraInfo, name) of the camera to use, or an exit code after printing why."""
    from ha_edge.camera import devices

    cams = devices.list_cameras()
    wanted = flag if flag is not None else cfg.camera_name
    if wanted is None:
        if len(cams) == 1:
            return cams[0]
        found = ", ".join(c.name for c in cams) or "none"
        _err(f"Which camera? Found: {found}. Use `--camera NAME` (see `ha-camera cameras`).")
        return EXIT_USAGE
    if flag is None:
        matches = [c for c in cams if c.name == wanted]  # the pinned name must match exactly
    else:
        matches = _match_camera(cams, wanted)
    if len(matches) == 1:
        return matches[0]
    found = ", ".join(c.name for c in cams) or "none"
    why = "matches more than one camera" if matches else "not found"
    _err(f"Camera {wanted!r} {why}. Available: {found}. Use `--camera NAME` to choose one.")
    return EXIT_USAGE if matches else EXIT_ERROR


def _load_models():
    """(pose, objects) from the model dir; an exit code after printing why (one line)."""
    from ha_edge.camera import detectors, models

    d = models.default_model_dir()
    if not (d / models.POSE_FILE).is_file() or not (d / models.OBJECT_FILE).is_file():
        _err("The camera models are missing. Run `ha-camera fetch-model` first.")
        return EXIT_ERROR
    pose = None
    try:
        import cv2  # noqa: F401 - fail early and clearly if the extra is missing

        pose = detectors.MediapipePose(d / models.POSE_FILE)
        return pose, detectors.MediapipeObjects(d / models.OBJECT_FILE)
    except ImportError:
        _err("The camera extra is not installed. Run `uv sync --extra camera`.")
    except Exception as e:  # noqa: BLE001 - any startup failure is one line, never a traceback
        _err(f"Cannot load the camera models: {type(e).__name__}: {e}")
    if pose is not None:
        pose.close()
    return EXIT_ERROR


def _open_camera(index: int):
    from ha_edge.camera.capture import CameraSource

    return CameraSource(index)


def _load_paired(command: str):
    from ha_edge import config

    try:
        cfg = config.load_camera()
    except config.ConfigError as e:
        _err(str(e))
        return EXIT_USAGE
    if cfg is None:
        _err("Not paired. Run `ha-camera pair --server URL --code CODE` first.")
        return EXIT_NOT_PAIRED
    return cfg


def _cmd_run(args: argparse.Namespace) -> int:
    from ha_edge import api, config
    from ha_edge.camera import runner

    cfg = _load_paired("run")
    if isinstance(cfg, int):
        return cfg
    cam = _pick_camera(args.camera, cfg)
    if isinstance(cam, int):
        return cam
    if (cfg.camera_name, cfg.camera_index) != (cam.name, cam.index):
        cfg.camera_name, cfg.camera_index = cam.name, cam.index
        config.save_camera(cfg)
    loaded = _load_models()
    if isinstance(loaded, int):
        return loaded
    pose, objects = loaded
    _setup_logging()
    print(f"Camera: {cam.name}")
    print(WATCHING, flush=True)

    async def main() -> int:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError, RuntimeError):
                loop.add_signal_handler(sig, stop.set)
        client = api.ServerClient(cfg.base_url, cfg.device_key)
        try:
            return await runner.run_camera(
                client, lambda: _open_camera(cam.index), pose, objects, stop=stop
            )
        finally:
            await client.aclose()

    return asyncio.run(main())


def _check_config(cfg):
    """The household's config (thresholds, enabled events, guarding), or None."""
    from ha_edge import api

    if cfg is None:
        print("Not paired: using defaults (every event on, guarding assumed on).")
        return None

    async def fetch():
        client = api.ServerClient(cfg.base_url, cfg.device_key)
        try:
            return await client.config()
        finally:
            await client.aclose()

    try:
        dc = asyncio.run(fetch())
    except api.ApiError as e:
        print(
            f"Cannot reach {cfg.base_url} ({e}): using defaults (every event on, guarding assumed on)."
        )
        return None
    print(f"Using your household's settings from {cfg.base_url}.")
    return dc


def _cmd_check(args: argparse.Namespace) -> int:
    from ha_edge import config
    from ha_edge.camera import check
    from ha_edge.camera.capture import CameraBusy

    try:
        cfg = config.load_camera()
    except config.ConfigError as e:
        _err(str(e))
        return EXIT_USAGE
    if cfg is None:
        cam = _pick_camera(args.camera, config.CameraConfig("", 0, ""))
    else:
        cam = _pick_camera(args.camera, cfg)
    if isinstance(cam, int):
        return cam
    dc = _check_config(cfg)
    loaded = _load_models()
    if isinstance(loaded, int):
        return loaded
    pose, objects = loaded
    try:
        try:
            source = _open_camera(cam.index)
        except CameraBusy:
            print(f"Camera {cam.name}: {check.advice(check.CheckSummary())}")
            return EXIT_ERROR
        except Exception as e:  # noqa: BLE001 - e.g. cv2 missing or a driver error: one line
            _err(f"Cannot open the camera {cam.name}: {type(e).__name__}: {e}")
            return EXIT_ERROR
        print(
            f"Looking through: {cam.name} for {args.seconds:g} s. Nothing is recorded or sent.",
            flush=True,
        )
        try:
            check.run_check(
                source,
                pose,
                objects,
                cfg=dc,
                seconds=args.seconds,
                out=lambda line: print(line, flush=True),
            )
        finally:
            source.close()
    finally:
        pose.close()
        objects.close()
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
    r = sub.add_parser("run", help="watch the camera and send alerts")
    r.add_argument("--camera", help="camera name (or unique part of it); saved for next time")
    c = sub.add_parser("check", help="show what the camera sees and would send; sends nothing")
    c.add_argument(
        "--seconds", type=_positive_seconds, default=15.0, help="how long to look (default 15)"
    )
    c.add_argument("--camera", help="camera name (default: the saved one); not saved")
    args = parser.parse_args(argv)
    commands = {
        "pair": _cmd_pair,
        "cameras": _cmd_cameras,
        "fetch-model": _cmd_fetch_model,
        "run": _cmd_run,
        "check": _cmd_check,
    }
    try:
        return commands[args.command](args)
    except KeyboardInterrupt:
        return EXIT_OK
