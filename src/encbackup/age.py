"""age: the repository password is stored encrypted to one or more age recipients."""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path

from encbackup import proc

# X25519 recipients: "age1" and 58 characters of the bech32 alphabet.
RECIPIENT = re.compile(r"^age1[02-9ac-hj-np-z]{58}$")
MIN_VERSION = (1, 0, 0)


def version() -> str:
    out = proc.run([proc.find("age"), "--version"]).out.decode().strip()
    return out


def check_recipient(value: str) -> str:
    if not RECIPIENT.match(value):
        raise proc.ToolError(
            "not an age recipient (expected an 'age1...' public key of 62 characters)"
        )
    return value


def check_identity_file(path: Path) -> None:
    """Refuse an identity file other users can read (as ssh does)."""
    try:
        mode = path.stat().st_mode
    except OSError as exc:
        raise proc.ToolError(f"cannot read the identity file {path}: {exc.strerror}") from exc
    if os.name == "posix" and mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise proc.ToolError(
            f"the identity file {path} is accessible to other users (mode {stat.S_IMODE(mode):04o}); run: chmod 600 {path}"
        )


def generate_identity(path: Path) -> str:
    """Create a new identity file (mode 0600) and return its public key."""
    if path.exists():
        raise proc.ToolError(f"{path} already exists; refusing to overwrite an identity")
    path.parent.mkdir(parents=True, exist_ok=True)
    proc.run([proc.find("age-keygen"), "-o", str(path)])
    if os.name == "posix":
        path.chmod(0o600)
    return public_key(path)


def public_key(identity: Path) -> str:
    out = proc.run([proc.find("age-keygen"), "-y", str(identity)]).out.decode().strip()
    return check_recipient(out)


def encrypt(data: bytes, recipients: list[str]) -> bytes:
    if not recipients:
        raise proc.ToolError("no recipients to encrypt to")
    argv = [proc.find("age"), "-a"]
    for r in recipients:
        argv += ["-r", check_recipient(r)]
    return proc.run(argv, stdin=data).out


def decrypt(blob: bytes, identity: Path) -> bytes:
    check_identity_file(identity)
    try:
        return proc.run([proc.find("age"), "-d", "-i", str(identity)], stdin=blob).out
    except proc.ToolError as exc:
        raise proc.ToolError(
            f"age could not decrypt with {identity}: {str(exc).split(': ', 1)[-1]}"
        ) from exc
