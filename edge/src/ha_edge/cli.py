"""Console entry points: ``ha-sensor`` and ``ha-agent``.

The device key is never printed or logged.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import signal
import sys
from collections.abc import Sequence

DISCLAIMER = "Home-awareness tool. Not a medical device, security alarm, or emergency service."

EXIT_OK = 0
EXIT_ERROR = 1  # network / model failures outside the sensor loop
EXIT_USAGE = 2
EXIT_NOT_PAIRED = 3
EXIT_MIC_LOST = 4
EXIT_REVOKED = 5  # returned by sensor.run_sensor (same values as ha_edge.sensor)


def _parser(prog: str, description: str) -> argparse.ArgumentParser:
    return argparse.ArgumentParser(
        prog=prog,
        description=description,
        epilog=DISCLAIMER,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )


def _err(msg: str) -> None:
    print(msg, file=sys.stderr)


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    # httpx logs every request at INFO; keep the output to our own events.
    logging.getLogger("httpx").setLevel(logging.WARNING)


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
    except api.ApiError as e:
        _err(f"Pairing failed: cannot reach the server at {args.server} ({e}).")
        return EXIT_ERROR
    mic_name = None
    with contextlib.suppress(config.ConfigError):
        old = config.load_sensor()
        mic_name = old.mic_name if old else None
    config.save_sensor(config.SensorConfig(args.server, device_id, key, mic_name))
    print(f"Paired as device {device_id}.")
    return EXIT_OK


def _cmd_devices(args: argparse.Namespace) -> int:
    from ha_edge import audio

    try:
        devices = audio.list_inputs()
    except RuntimeError as e:
        _err(f"Cannot list microphones: {e}")
        return EXIT_ERROR
    if not devices:
        print("No microphones found.")
        return EXIT_OK
    width = max(len(d.name) for d in devices)
    print(f"  {'INDEX':>5}  {'NAME':<{width}}  DESCRIPTION")
    for d in devices:
        mark = "*" if d.is_default else " "
        print(f"{mark} {d.index:>5}  {d.name:<{width}}  {d.description}")
    print("(* = default input)")
    return EXIT_OK


def _cmd_run(args: argparse.Namespace) -> int:
    from ha_edge import api, audio, config, model, playback, sensor

    try:
        cfg = config.load_sensor()
    except config.ConfigError as e:
        _err(str(e))
        return EXIT_USAGE
    if cfg is None:
        _err("Not paired. Run `ha-sensor pair --server URL --code CODE` first.")
        return EXIT_NOT_PAIRED

    try:
        devices = audio.list_inputs()
    except RuntimeError as e:
        _err(f"Cannot list microphones: {e}")
        return EXIT_ERROR
    wanted = args.mic if args.mic is not None else cfg.mic_name
    if args.mic is None and cfg.mic_name is not None:
        # A pinned name must match exactly: never drift to a similarly named input.
        devices_to_search = [d for d in devices if d.name == cfg.mic_name]
    else:
        devices_to_search = devices
    try:
        device = audio.resolve_device(wanted, devices_to_search)
    except audio.MicNotFound as e:
        which = repr(e.name) if e.name else "default microphone"
        _err(
            f"Microphone {which} not found; not switching to another input. "
            f"Available: {', '.join(d.name for d in devices) or 'none'}. "
            "Use `ha-sensor run --mic NAME` to choose one."
        )
        return EXIT_MIC_LOST
    if cfg.mic_name != device.name:
        cfg.mic_name = device.name
        config.save_sensor(cfg)
    print(f"Listening on: {device.description or device.name}")

    _setup_logging()
    import httpx

    try:
        classifier = model.YamnetClassifier(model.fetch_model())
    except (OSError, ValueError, RuntimeError, httpx.HTTPError, model.ModelIntegrityError) as e:
        _err(f"Cannot load the sound model: {e}")
        return EXIT_ERROR

    async def main() -> int:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError, RuntimeError):
                loop.add_signal_handler(sig, stop.set)
        client = api.ServerClient(cfg.base_url, cfg.device_key)
        try:
            return await sensor.run_sensor(
                cfg,
                client=client,
                classifier=classifier,
                mic=audio.MicStream(device),
                probe=playback.default_probe(),
                stop=stop,
            )
        finally:
            await client.aclose()

    return asyncio.run(main())


def sensor_main(argv: Sequence[str] | None = None) -> int:
    parser = _parser("ha-sensor", "Turn this laptop into a sound sensor.")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("pair", help="pair this laptop with your Home Awareness account")
    p.add_argument("--server", required=True, help="server URL, e.g. https://example.org")
    p.add_argument("--code", required=True, help="pairing code from the web app")
    sub.add_parser("devices", help="list microphones")
    sub.add_parser("fetch-model", help="download and verify the YAMNet model")
    r = sub.add_parser("run", help="listen and send detected sounds")
    r.add_argument("--mic", help="microphone name (or unique part of it); saved for next time")
    args = parser.parse_args(argv)
    commands = {
        "pair": _cmd_pair,
        "devices": _cmd_devices,
        "run": _cmd_run,
        "fetch-model": _cmd_fetch_model,
    }
    try:
        return commands[args.command](args)
    except KeyboardInterrupt:  # Ctrl-C before the loop installs its signal handlers
        return EXIT_OK


def _cmd_fetch_model(args: argparse.Namespace) -> int:
    import httpx

    from ha_edge import model

    try:
        path = model.fetch_model(model.default_model_dir())
    except (OSError, httpx.HTTPError, model.ModelIntegrityError) as e:
        _err(f"Cannot download the sound model: {e}")
        return EXIT_ERROR
    print(path)
    return EXIT_OK


def agent_main(argv: Sequence[str] | None = None) -> int:
    _parser("ha-agent", "Show Home Awareness alerts on this laptop.").parse_args(argv)
    return EXIT_OK
