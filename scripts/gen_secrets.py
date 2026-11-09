"""Create deploy/.env.prod from deploy/.env.prod.example with fresh secrets.

Prints only the output path, never a secret value. The file is written atomically with mode
0600; it is never overwritten unless --force or --rotate is given, and a symlink target is
refused.
"""

import argparse
import base64
import contextlib
import os
import secrets
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "deploy" / ".env.prod.example"
SECRET_KEYS = ("POSTGRES_PASSWORD", "JWT_SECRET", "SNAPSHOT_KEY")


def generate() -> dict[str, str]:
    return {
        "POSTGRES_PASSWORD": secrets.token_urlsafe(32),
        "JWT_SECRET": secrets.token_urlsafe(48),
        "SNAPSHOT_KEY": base64.urlsafe_b64encode(
            os.urandom(32)
        ).decode(),  # a valid Fernet key is 32 random bytes
    }


def parse(text: str) -> dict[str, str]:
    values = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep and not line.startswith("#"):
            values[key] = value
    return values


def render(template: str, values: dict[str, str]) -> str:
    lines = []
    for line in template.splitlines():
        key, sep, _ = line.partition("=")
        lines.append(f"{key}={values[key]}" if sep and key in values else line)
    return "\n".join(lines) + "\n"


def write_atomic(path: Path, content: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")  # 0600
    try:
        with os.fdopen(fd, "w") as f:
            os.fchmod(f.fileno(), 0o600)
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=ROOT / "deploy" / ".env.prod")
    parser.add_argument(
        "--force",
        action="store_true",
        help="rewrite an existing file from the template but KEEP its existing "
        "POSTGRES_PASSWORD, JWT_SECRET and SNAPSHOT_KEY (only missing/empty ones are generated)",
    )
    parser.add_argument(
        "--rotate",
        action="store_true",
        help="regenerate ALL secrets in an existing file. WARNING: the existing database volume "
        "keeps the old POSTGRES_PASSWORD so the api cannot connect until it is changed there; "
        "stored snapshots (old SNAPSHOT_KEY) become unreadable; changing JWT_SECRET gets "
        "everyone logged out",
    )
    args = parser.parse_args(argv)

    out: Path = args.out
    if out.is_symlink():
        print(f"{out} is a symlink; refusing to write through it", file=sys.stderr)
        return 1
    exists = out.exists()
    if exists and not (args.force or args.rotate):
        print(
            f"{out} already exists; use --force to refresh it (secrets are kept)", file=sys.stderr
        )
        return 1

    values = generate()
    if exists and not args.rotate:
        old = parse(out.read_text())
        values.update({k: old[k] for k in SECRET_KEYS if old.get(k)})
    write_atomic(out, render(EXAMPLE.read_text(), values))
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
