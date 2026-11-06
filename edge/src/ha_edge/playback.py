"""Detects whether this machine is playing audio through its speakers.

While the laptop plays a movie through speakers the microphone hears it, so the
detector raises its thresholds. The probe reads ``pw-dump`` (PipeWire).
"""

import json
import logging
import shutil
import subprocess
import time
from collections.abc import Callable
from typing import Any, Protocol

log = logging.getLogger(__name__)

_OUTPUT_STREAM = "Stream/Output/Audio"
_HEADSET_FORM_FACTORS = {"headphone", "headset"}
_HEADSET_WORDS = ("headphone", "headset")


class PlaybackProbe(Protocol):
    def playing(self) -> bool: ...


class NullProbe:
    """Never reports playback (non-PipeWire platforms)."""

    def playing(self) -> bool:
        return False


def _is_headset_text(*values: object) -> bool:
    return any(isinstance(v, str) and any(w in v.lower() for w in _HEADSET_WORDS) for v in values)


class PipeWireProbe:
    """True while an output stream is running AND the default sink is not headphones.

    A node with ``media.class`` "Stream/Output/Audio" and ``info.state`` "running"
    means something is playing. If the default sink (from the "default" metadata,
    ``default.audio.sink``) is a headset/headphones the microphone cannot hear it,
    so ``playing()`` is False. Headphones are recognised from, case-insensitively:
    ``device.form-factor`` in {headphone, headset} on the sink node or its device;
    "headphone"/"headset" in the sink's ``node.description``,
    ``device.profile.description`` or ``api.alsa.path``; or in the name/description
    of the device's active output Route (the built-in jack case, where the
    form factor stays "internal"). If the default sink cannot be determined,
    speakers are assumed. Any failure (missing binary, nonzero exit, bad JSON,
    timeout) yields False and is logged once per failure kind.
    """

    def __init__(
        self,
        run: Callable[..., Any] = subprocess.run,
        clock: Callable[[], float] = time.monotonic,
        cache_s: float = 2.0,
    ) -> None:
        self._run = run
        self._clock = clock
        self._cache_s = cache_s
        self._cached: bool = False
        self._cached_at: float | None = None
        self._logged: set[str] = set()

    def playing(self) -> bool:
        now = self._clock()
        if self._cached_at is not None and now - self._cached_at < self._cache_s:
            return self._cached
        self._cached = self._probe()
        self._cached_at = now
        return self._cached

    def _fail(self, kind: str, detail: str) -> bool:
        if kind not in self._logged:
            self._logged.add(kind)
            log.warning("pw-dump playback probe failed (%s): %s", kind, detail)
        return False

    def _probe(self) -> bool:
        try:
            proc = self._run(["pw-dump"], capture_output=True, text=True, timeout=1.0, check=False)
        except FileNotFoundError as e:
            return self._fail("missing", str(e))
        except subprocess.TimeoutExpired:
            return self._fail("timeout", "timed out after 1 s")
        except OSError as e:
            return self._fail("os-error", str(e))
        if proc.returncode != 0:
            return self._fail("exit", f"exit status {proc.returncode}")
        try:
            objects = json.loads(proc.stdout)
        except (ValueError, TypeError):
            return self._fail("bad-json", "output is not valid JSON")
        if not isinstance(objects, list):
            return self._fail("bad-json", "unexpected JSON shape")
        try:
            return self._decide(objects)
        except (KeyError, TypeError, AttributeError):
            return self._fail("bad-shape", "unexpected pw-dump structure")

    def _decide(self, objects: list[dict]) -> bool:
        running = any(
            _props(o).get("media.class") == _OUTPUT_STREAM
            and (o.get("info") or {}).get("state") == "running"
            for o in objects
        )
        return running and not _default_sink_is_headset(objects)


def _props(obj: dict) -> dict:
    return (obj.get("info") or {}).get("props") or {}


def _default_sink_name(objects: list[dict]) -> str | None:
    for o in objects:
        if (o.get("props") or {}).get("metadata.name") != "default":
            continue
        for entry in o.get("metadata") or []:
            if entry.get("key") == "default.audio.sink":
                value = entry.get("value")
                if isinstance(value, dict):
                    value = value.get("name")
                return value if isinstance(value, str) else None
    return None


def _default_sink_is_headset(objects: list[dict]) -> bool:
    name = _default_sink_name(objects)
    if name is None:
        return False
    sink = next(
        (
            o
            for o in objects
            if _props(o).get("node.name") == name
            and str(_props(o).get("media.class", "")).startswith("Audio/Sink")
        ),
        None,
    )
    if sink is None:
        return False
    sp = _props(sink)
    if str(sp.get("device.form-factor", "")).lower() in _HEADSET_FORM_FACTORS:
        return True
    if _is_headset_text(
        sp.get("node.description"), sp.get("device.profile.description"), sp.get("api.alsa.path")
    ):
        return True
    device = next(
        (
            o
            for o in objects
            if o.get("id") == sp.get("device.id") and "Device" in str(o.get("type", ""))
        ),
        None,
    )
    if device is None:
        return False
    if str(_props(device).get("device.form-factor", "")).lower() in _HEADSET_FORM_FACTORS:
        return True
    pcm = sp.get("card.profile.device")
    for route in ((device.get("info") or {}).get("params") or {}).get("Route") or []:
        if route.get("direction") != "Output":
            continue
        if pcm is not None and route.get("device") != pcm:
            continue
        if _is_headset_text(route.get("name"), route.get("description")):
            return True
    return False


def default_probe() -> PlaybackProbe:
    return PipeWireProbe() if shutil.which("pw-dump") else NullProbe()
