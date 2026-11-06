"""Microphone capture (PipeWire CLI backend), device pinning and windowing.

Raw audio stays in memory only. A pinned device that disappears raises MicLost;
we never fall back to a different input.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import json
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from typing import Self

import numpy as np

SAMPLE_RATE = 16000
READ_BYTES = 8192


class MicNotFound(Exception):
    def __init__(self, name: str | None, available: list[str]):
        self.name = name
        self.available = available
        super().__init__(f"microphone {name!r} not found; available: {available}")


class MicLost(Exception):
    """The capture stream ended or failed (e.g. device unplugged)."""


@dataclass
class InputDevice:
    index: int
    name: str
    default_samplerate: float
    is_default: bool
    description: str = ""


def list_inputs(run: Callable = subprocess.run) -> list[InputDevice]:
    try:
        proc = run(["pw-dump"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"pw-dump failed: {exc}") from exc
    if proc.returncode != 0:
        raise RuntimeError(f"pw-dump failed: {(proc.stderr or '').strip()}")
    objects = json.loads(proc.stdout)
    default_name = None
    for obj in objects:
        if (
            obj.get("type", "").endswith("Metadata")
            and obj.get("props", {}).get("metadata.name") == "default"
        ):
            for entry in obj.get("metadata", []):
                if entry.get("key") == "default.audio.source":
                    value = entry.get("value")
                    default_name = value.get("name") if isinstance(value, dict) else value
    devices = []
    for obj in objects:
        if not obj.get("type", "").endswith("Node"):
            continue
        props = (obj.get("info") or {}).get("props") or {}
        if props.get("media.class") != "Audio/Source" or "node.name" not in props:
            continue
        devices.append(
            InputDevice(
                index=obj["id"],
                name=props["node.name"],
                default_samplerate=float(props.get("audio.rate") or 48000),
                is_default=props["node.name"] == default_name,
                description=props.get("node.description") or props.get("node.nick") or "",
            )
        )
    return devices


def resolve_device(name: str | None, devices: list[InputDevice]) -> InputDevice:
    available = [d.name for d in devices]
    if name is not None and not name.strip():
        raise MicNotFound(name, available)
    if name is None:
        for d in devices:
            if d.is_default:
                return d
        raise MicNotFound(None, available)
    for d in devices:
        if d.name == name:
            return d
    needle = name.lower()
    matches = [d for d in devices if needle in d.name.lower() or needle in d.description.lower()]
    if len(matches) == 1:
        return matches[0]
    raise MicNotFound(name, available)


class Windower:
    def __init__(self, window: int = 15600, hop: int = 7800):
        self.window = window
        self.hop = hop
        self._buf = np.zeros(0, dtype=np.float32)

    def push(self, samples: np.ndarray) -> list[np.ndarray]:
        self._buf = np.concatenate([self._buf, np.asarray(samples, dtype=np.float32)])
        out = []
        while len(self._buf) >= self.window:
            out.append(self._buf[: self.window].copy())
            self._buf = self._buf[self.hop :]
        return out


def resample_linear(x: np.ndarray, src_rate: float, dst_rate: float) -> np.ndarray:
    """Linear-interpolation resample (for backends that cannot open at 16 kHz)."""
    if src_rate == dst_rate:
        return x
    n_out = round(len(x) * dst_rate / src_rate)
    if len(x) == 0 or n_out == 0:
        return np.zeros(0, dtype=np.float32)
    pos = np.arange(n_out) * (src_rate / dst_rate)
    return np.interp(pos, np.arange(len(x)), x).astype(np.float32)


PIPEWIRE_PINNING = "{ node.dont-fallback=true node.dont-reconnect=true node.dont-move=true }"
STDERR_KEEP = 2048


class MicStream:
    """Async iterator of 16 kHz mono float32 chunks from one pinned device.

    PipeWire (pw-record) does the resampling. The stream properties stop PipeWire
    from moving/reconnecting it to another device, so unplugging ends the process.
    Raises MicLost if the process exits, stdout ends, or no audio arrives for
    `no_audio_timeout` seconds; never starts a different device.
    """

    def __init__(
        self,
        device: InputDevice,
        *,
        spawn=asyncio.create_subprocess_exec,
        no_audio_timeout: float = 3.0,
        kill_after: float = 2.0,
    ):
        self.device = device
        self._spawn = spawn
        self._no_audio_timeout = no_audio_timeout
        self._kill_after = kill_after
        self._proc = None
        self._rest = b""
        self._closed = False
        self._stderr = collections.deque(maxlen=STDERR_KEEP)
        self._drain_task: asyncio.Task | None = None

    async def __aenter__(self) -> Self:
        try:
            self._proc = await self._spawn(
                "pw-record", "--target", self.device.name,
                "-P", PIPEWIRE_PINNING,
                "--rate", str(SAMPLE_RATE), "--channels", "1", "--format", "f32", "-",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                stdin=asyncio.subprocess.DEVNULL,
            )  # fmt: skip
        except OSError as exc:
            raise MicLost(f"cannot start pw-record: {exc}") from exc
        self._drain_task = asyncio.create_task(self._drain_stderr(self._proc.stderr))
        return self

    async def _drain_stderr(self, stream) -> None:
        try:
            while data := await stream.read(4096):
                self._stderr.extend(data)
        except (OSError, ValueError):
            pass

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    def __aiter__(self) -> Self:
        return self

    async def __anext__(self) -> np.ndarray:
        if self._closed:
            raise StopAsyncIteration
        if self._proc is None:
            await self.__aenter__()
        while True:
            try:
                data = await asyncio.wait_for(
                    self._proc.stdout.read(READ_BYTES), self._no_audio_timeout
                )
            except TimeoutError:
                await self.aclose()
                raise MicLost(f"no audio for {self._no_audio_timeout:g} s") from None
            except (OSError, ValueError) as exc:
                await self.aclose()
                raise MicLost(f"microphone read failed: {exc}") from exc
            if not data:
                await asyncio.sleep(0)  # let the stderr drain see EOF
                tail = await self._stderr_tail()
                await self.aclose()
                raise MicLost(f"microphone {self.device.name!r} stopped: {tail}")
            data = self._rest + data
            usable = len(data) - len(data) % 4
            self._rest = data[usable:]
            if usable:
                return np.frombuffer(data[:usable], dtype="<f4").astype(np.float32)

    async def _stderr_tail(self) -> str:
        task = self._drain_task
        if task is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(asyncio.shield(task), timeout=0.5)
        return bytes(self._stderr).decode(errors="replace").strip()

    async def aclose(self) -> None:
        self._closed = True
        proc, self._proc = self._proc, None
        task, self._drain_task = self._drain_task, None
        if task is not None:
            task.cancel()
        if proc is None or proc.returncode is not None:
            return
        try:
            with contextlib.suppress(ProcessLookupError):
                proc.terminate()
            await asyncio.wait_for(proc.wait(), timeout=self._kill_after)
        except TimeoutError:
            pass
        finally:
            if proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(proc.wait(), timeout=self._kill_after)
