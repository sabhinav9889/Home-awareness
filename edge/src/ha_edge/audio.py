"""Microphone capture (PipeWire CLI backend), device pinning and windowing.

Raw audio stays in memory only. A pinned device that disappears raises MicLost;
we never fall back to a different input.
"""

from __future__ import annotations

import asyncio
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


class MicStream:
    """Async iterator of 16 kHz mono float32 chunks from one pinned device.

    PipeWire (pw-record) does the resampling. Raises MicLost if the process
    exits or its stdout ends; never starts a different device.
    """

    def __init__(self, device: InputDevice, *, spawn=asyncio.create_subprocess_exec):
        self.device = device
        self._spawn = spawn
        self._proc = None
        self._rest = b""

    async def __aenter__(self) -> Self:
        self._proc = await self._spawn(
            "pw-record", "--target", self.device.name, "--rate", str(SAMPLE_RATE),
            "--channels", "1", "--format", "f32", "-",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.DEVNULL,
        )  # fmt: skip
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    def __aiter__(self) -> Self:
        return self

    async def __anext__(self) -> np.ndarray:
        if self._proc is None:
            await self.__aenter__()
        while True:
            try:
                data = await self._proc.stdout.read(READ_BYTES)
            except (OSError, ValueError) as exc:
                raise MicLost(f"microphone read failed: {exc}") from exc
            if not data:
                raise MicLost(
                    f"microphone {self.device.name!r} stopped: {await self._stderr_tail()}"
                )
            data = self._rest + data
            usable = len(data) - len(data) % 4
            self._rest = data[usable:]
            if usable:
                return np.frombuffer(data[:usable], dtype="<f4").astype(np.float32)

    async def _stderr_tail(self) -> str:
        try:
            raw = await asyncio.wait_for(self._proc.stderr.read(2000), timeout=1)
        except (OSError, ValueError, TimeoutError):
            return ""
        return raw.decode(errors="replace").strip()[-300:]

    async def aclose(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None or proc.returncode is not None:
            return
        with contextlib.suppress(ProcessLookupError):
            proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=2)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await proc.wait()
