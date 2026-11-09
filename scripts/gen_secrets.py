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


def render(template: str, values: dict[str, str]) -> str:
    lines = []
    for line in template.splitlines():
        key, sep, _ = line.partition("=")
        lines.append(f"{key}={values[key]}" if sep and key in values else line)
    return "\n".join(lines) + "\n"


def merge(existing: str, template: str, fresh: dict[str, str], rotate: bool) -> str:
    """Keep the existing file as it is; change only what must change.

    Every existing line (settings, unknown keys, comments) is kept verbatim. Only the three
    secrets are replaced, when empty/missing (--force) or always (--rotate). Keys that are new
    in the template are appended.
    """
    lines = []
    seen: set[str] = set()
    for line in existing.splitlines():
        key, sep, value = line.partition("=")
        if sep and not line.startswith("#") and key in SECRET_KEYS:
            seen.add(key)
            if rotate or not value.strip():
                line = f"{key}={fresh[key]}"
        elif sep and not line.startswith("#"):
            seen.add(key)
        lines.append(line)
    added = []
    for line in template.splitlines():
        key, sep, _ = line.partition("=")
        if sep and not line.startswith("#") and key not in seen:
            added.append(f"{key}={fresh[key]}" if key in SECRET_KEYS else line)
    if added:
        lines += ["", "# Added by scripts/gen_secrets.py (new in the template):", *added]
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
        help="refresh an existing file: KEEP every existing setting and secret line, generate "
        "only POSTGRES_PASSWORD, JWT_SECRET and SNAPSHOT_KEY when missing/empty, and add keys "
        "that are new in the template",
    )
    parser.add_argument(
        "--rotate",
        action="store_true",
        help="regenerate the three secrets (POSTGRES_PASSWORD, JWT_SECRET, SNAPSHOT_KEY) in an "
        "existing file and keep all other settings. WARNING: the existing database volume "
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
    if exists:
        content = merge(out.read_text(), EXAMPLE.read_text(), values, rotate=args.rotate)
    else:
        content = render(EXAMPLE.read_text(), values)
    write_atomic(out, content)
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
