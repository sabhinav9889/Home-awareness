"""Console entry points. Subcommands are added in later tasks."""

from __future__ import annotations

import argparse
from collections.abc import Sequence

DISCLAIMER = "Home-awareness tool. Not a medical device, security alarm, or emergency service."


def _parser(prog: str, description: str) -> argparse.ArgumentParser:
    return argparse.ArgumentParser(
        prog=prog,
        description=description,
        epilog=DISCLAIMER,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )


def sensor_main(argv: Sequence[str] | None = None) -> int:
    _parser("ha-sensor", "Turn this laptop into a sound sensor.").parse_args(argv)
    return 0


def agent_main(argv: Sequence[str] | None = None) -> int:
    _parser("ha-agent", "Show Home Awareness alerts on this laptop.").parse_args(argv)
    return 0
