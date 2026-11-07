"""Console entry points: ``ha-sensor`` and ``ha-agent``.

The device key, the agent's password and its tokens are never printed or logged.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import getpass
import logging
import signal
import socket
import sys
import time
from collections.abc import Sequence
from typing import Any

DISCLAIMER = "Home-awareness tool. Not a medical device, security alarm, or emergency service."

EXIT_OK = 0
EXIT_ERROR = 1  # network / model failures outside the sensor loop
EXIT_USAGE = 2
EXIT_NOT_PAIRED = 3
EXIT_MIC_LOST = 4
EXIT_REVOKED = 5  # returned by sensor.run_sensor (same values as ha_edge.sensor)
EXIT_NOT_LOGGED_IN = 3
EXIT_RELOGIN = 6  # returned by agent.run_agent: the server rejected the stored token


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


def _cmd_login(args: argparse.Namespace) -> int:
    import httpx

    base = args.server.rstrip("/")
    try:
        password = getpass.getpass(f"Password for {args.email}: ")
    except (KeyboardInterrupt, EOFError):
        print(file=sys.stderr)
        return EXIT_OK
    with httpx.Client(timeout=15) as http:
        try:
            r = http.post(f"{base}/auth/login", json={"email": args.email, "password": password})
        except httpx.HTTPError as e:
            _err(f"Login failed: cannot reach the server at {base} ({type(e).__name__}).")
            return EXIT_ERROR
        del password
        return _finish_login(http, base, args.email, r)


def _finish_login(http: Any, base: str, email: str, r: Any) -> int:
    import httpx

    from ha_edge import config

    if r.status_code == 401:
        _err("Login failed: wrong email or password.")
        return EXIT_USAGE
    if r.status_code == 403:
        _err("Login failed: email not confirmed. Use the link in the confirmation email.")
        return EXIT_USAGE
    if r.status_code == 429:
        _err("Login failed: Too many attempts. Wait a minute and try again.")
        return EXIT_USAGE
    jwt = None
    if r.status_code == 200:
        with contextlib.suppress(ValueError, AttributeError):
            jwt = r.json().get("access_token")
    if not isinstance(jwt, str) or not jwt:
        _err(f"Login failed: unexpected server response (HTTP {r.status_code}).")
        return EXIT_ERROR
    auth = {"Authorization": f"Bearer {jwt}"}
    # Swap the 24 h session for a long-lived, revocable agent token; only that is stored.
    host = socket.gethostname().strip()[:90] or "this laptop"
    try:
        t = http.post(f"{base}/me/agent-tokens", json={"name": f"{host} agent"}, headers=auth)
    except httpx.HTTPError as e:
        _err(f"Login failed: cannot reach the server at {base} ({type(e).__name__}).")
        return EXIT_ERROR
    if t.status_code == 409:
        _err(
            "Login failed: this account already has 10 agent tokens. Revoke one you no longer "
            "use (DELETE /me/agent-tokens/{id}) and try again."
        )
        return EXIT_USAGE
    if t.status_code == 429:
        _err("Login failed: Too many attempts. Wait a minute and try again.")
        return EXIT_USAGE
    agent_token = None
    agent_token_id = None
    if t.status_code == 201:
        with contextlib.suppress(ValueError, AttributeError):
            body = t.json()
            agent_token = body.get("token")
            tid = body.get("id")
            agent_token_id = tid if isinstance(tid, int) and not isinstance(tid, bool) else None
    if not isinstance(agent_token, str) or not agent_token:
        _err(f"Login failed: unexpected server response (HTTP {t.status_code}).")
        return EXIT_ERROR
    # member_id lets the agent recognise its own acks; without it, it compares emails.
    member_id = None
    with contextlib.suppress(httpx.HTTPError, ValueError, AttributeError):
        me = http.get(f"{base}/auth/me", headers=auth)
        if me.status_code == 200:
            mid = me.json().get("id")
            member_id = mid if isinstance(mid, int) and not isinstance(mid, bool) else None
    # Save first, revoke after: if the save fails the stored login must still work, and the
    # new token must not be left live on the server with nothing holding it.
    previous_id = _previous_token_id(base)
    try:
        config.save_agent(
            config.AgentConfig(
                base,
                email,
                member_id=member_id,
                agent_token=agent_token,
                agent_token_id=agent_token_id,
            )
        )
    except OSError as e:
        _revoke_token(http, base, auth, agent_token_id)
        _err(f"Login failed: cannot save the login in {config.config_dir()}: {e.strerror or e}.")
        return EXIT_ERROR
    _revoke_token(http, base, auth, previous_id)
    print(f"Logged in as {email}. Now run `ha-agent run`.")
    return EXIT_OK


def _previous_token_id(base: str) -> int | None:
    """The stored agent token's id, if it belongs to this server; None on any config problem."""
    from ha_edge import config

    with contextlib.suppress(config.ConfigError, OSError):
        old = config.load_agent()
        if old is not None and old.base_url == base:
            return old.agent_token_id
    return None


def _revoke_token(http: Any, base: str, auth: dict[str, str], token_id: int | None) -> None:
    """Best effort: a re-login replaces the stored agent token, so revoke the replaced one (or
    the new one, if it could not be saved) instead of leaving it live. Any failure is
    ignored; nothing is printed."""
    import httpx

    if token_id is None:
        return
    with contextlib.suppress(httpx.HTTPError):
        http.delete(f"{base}/me/agent-tokens/{token_id}", headers=auth)


def _cmd_agent_run(args: argparse.Namespace) -> int:
    import httpx

    from ha_edge import agent, config, notify

    try:
        cfg = config.load_agent()
    except config.ConfigError as e:
        _err(str(e))
        return EXIT_USAGE
    if cfg is None or not cfg.bearer_token:
        _err("Not logged in. Run `ha-agent login --server URL --email EMAIL` first.")
        return EXIT_NOT_LOGGED_IN
    _setup_logging()
    if cfg.agent_token is None:
        # A config saved before agent tokens: a 24 h JWT. Keep working, but say how to stop it.
        from datetime import UTC, datetime

        _err("This login expires daily: re-run `ha-agent login` to stop daily logins.")
        agent.log_login_expiry(cfg.access_token, datetime.now(UTC))

    async def main() -> int:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError, RuntimeError):
                loop.add_signal_handler(sig, stop.set)
        alarm = notify.AlarmPlayer()
        async with httpx.AsyncClient(timeout=httpx.Timeout(15, read=None)) as http:
            try:
                return await agent.run_agent(
                    cfg,
                    connect=agent.ws_connector(http),
                    notifier=notify.DesktopNotifier(),
                    alarm=alarm,
                    clock=time.monotonic,
                    http=http,
                    stop=stop,
                )
            finally:
                await alarm.aclose()

    print(f"Listening for alerts from {cfg.base_url} as {cfg.email}. Ctrl-C to stop.", flush=True)
    rc = asyncio.run(main())
    if rc == EXIT_RELOGIN:
        _err("The server rejected this login. Run `ha-agent login` again.")
    return rc


def _cmd_test_alarm(args: argparse.Namespace) -> int:
    from ha_edge import notify

    async def main() -> int:
        player = notify.AlarmPlayer(max_s=3.0)
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError, RuntimeError):
                loop.add_signal_handler(sig, player.stop)
        await player.play()
        if player.unavailable:
            _err("Cannot play the alarm: pw-play (PipeWire) is not available.")
            return EXIT_ERROR
        await player.wait()
        return EXIT_OK

    print("Playing the alarm for 3 s on the default output…", flush=True)
    return asyncio.run(main())


def agent_main(argv: Sequence[str] | None = None) -> int:
    parser = _parser("ha-agent", "Show Home Awareness alerts on this laptop.")
    sub = parser.add_subparsers(dest="command", required=True)
    lg = sub.add_parser(
        "login", help="log in; stores only a long-lived, revocable agent token (not the password)"
    )
    lg.add_argument("--server", required=True, help="server URL, e.g. https://example.org")
    lg.add_argument("--email", required=True, help="your Home Awareness email")
    sub.add_parser("run", help="show alerts as popups and sound the alarm")
    sub.add_parser("test-alarm", help="play the alarm for 3 s on the default output")
    args = parser.parse_args(argv)
    commands = {"login": _cmd_login, "run": _cmd_agent_run, "test-alarm": _cmd_test_alarm}
    try:
        return commands[args.command](args)
    except KeyboardInterrupt:  # Ctrl-C before the loop installs its signal handlers
        return EXIT_OK
