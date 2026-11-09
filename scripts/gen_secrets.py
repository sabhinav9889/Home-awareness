"""Create deploy/.env.prod from deploy/.env.prod.example with fresh secrets.

Prints only the output path, never a secret value. The file is created with mode 0600 and an
existing file is never overwritten unless --force is given.
"""

import argparse
import os
import secrets
import sys
from pathlib import Path

from cryptography.fernet import Fernet

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "deploy" / ".env.prod.example"


def generate() -> dict[str, str]:
    return {
        "POSTGRES_PASSWORD": secrets.token_urlsafe(32),
        "JWT_SECRET": secrets.token_urlsafe(48),
        "SNAPSHOT_KEY": Fernet.generate_key().decode(),
    }


def render(template: str, values: dict[str, str]) -> str:
    lines = []
    for line in template.splitlines():
        key, sep, _ = line.partition("=")
        lines.append(f"{key}={values[key]}" if sep and key in values else line)
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=ROOT / "deploy" / ".env.prod")
    parser.add_argument("--force", action="store_true", help="overwrite an existing file")
    args = parser.parse_args(argv)

    content = render(EXAMPLE.read_text(), generate())
    flags = os.O_WRONLY | os.O_CREAT | (os.O_TRUNC if args.force else os.O_EXCL)
    try:
        fd = os.open(args.out, flags, 0o600)
    except FileExistsError:
        print(f"{args.out} already exists; use --force to replace it", file=sys.stderr)
        return 1
    with os.fdopen(fd, "w") as f:
        os.fchmod(f.fileno(), 0o600)
        f.write(content)
    print(args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
