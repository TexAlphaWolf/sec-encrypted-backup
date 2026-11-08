"""The vault: the restic password, encrypted with age, plus who may open it and what was done."""

from __future__ import annotations

import json
import os
import secrets
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from encbackup import age
from encbackup.config import Config
from encbackup.proc import ToolError
from encbackup.restic import Restic

ROLES = ("host", "recovery")


def new_password() -> str:
    return secrets.token_urlsafe(48)  # 384 bits


def atomic_write(path: Path, data: bytes, mode: int = 0o600) -> None:
    """Write next to the target, flush to disk, then rename: a crash leaves the old file intact."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if os.name == "posix":
            os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def read_recipients(cfg: Config) -> list[tuple[str, str]]:
    """(role, public key) for every recipient the password is encrypted to."""
    try:
        text = cfg.recipients_file.read_text(encoding="utf-8")
    except OSError as exc:
        raise ToolError(
            f"cannot read {cfg.recipients_file}: {exc.strerror} (was 'init' run?)"
        ) from exc
    found: list[tuple[str, str]] = []
    for number, line in enumerate(text.splitlines(), 1):
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) != 2 or parts[0] not in ROLES:
            raise ToolError(
                f"{cfg.recipients_file}:{number}: expected 'host <key>' or 'recovery <key>'"
            )
        found.append((parts[0], age.check_recipient(parts[1])))
    if not any(role == "host" for role, _ in found):
        raise ToolError(f"{cfg.recipients_file} has no host recipient")
    return found


def write_recipients(cfg: Config, recipients: list[tuple[str, str]]) -> None:
    body = "# who can unlock the repository password (age public keys, safe to share)\n"
    body += "".join(f"{role} {key}\n" for role, key in recipients)
    atomic_write(cfg.recipients_file, body.encode(), 0o644)


def unlock(cfg: Config, identity: Path | None = None) -> str:
    """Decrypt the repository password with an age identity (the host's unless one is given)."""
    try:
        blob = cfg.password_file.read_bytes()
    except OSError as exc:
        raise ToolError(
            f"cannot read {cfg.password_file}: {exc.strerror} (was 'init' run?)"
        ) from exc
    plain = age.decrypt(blob, identity or cfg.identity)
    try:
        return plain.decode("ascii").strip()
    except UnicodeDecodeError as exc:
        raise ToolError("the decrypted repository password is not what encbackup writes") from exc


def restic_for(cfg: Config, identity: Path | None = None) -> Restic:
    return Restic(cfg.repository, unlock(cfg, identity), cfg.cache_dir, cfg.timeout)


def init(cfg: Config, recovery: list[str]) -> dict[str, Any]:
    """Create the identity, the password, the repository. Nothing is overwritten."""
    for existing in (cfg.password_file, cfg.recipients_file):
        if existing.exists():
            raise ToolError(f"{existing} already exists; this vault is already initialised")
    if not recovery:
        raise ToolError(
            "give at least one --recovery-recipient: a key kept offline, so that losing this machine does not lose the backups"
        )
    for r in recovery:
        age.check_recipient(r)
    created_identity = not cfg.identity.exists()
    host = age.generate_identity(cfg.identity) if created_identity else age.public_key(cfg.identity)
    if host in recovery:
        raise ToolError(
            "the recovery recipient must be a different key from this machine's identity"
        )
    recipients = [("host", host), *(("recovery", r) for r in recovery)]
    password = new_password()
    blob = age.encrypt(password.encode("ascii"), [k for _, k in recipients])
    cfg.vault.mkdir(parents=True, exist_ok=True)
    restic = Restic(cfg.repository, password, cfg.cache_dir, cfg.timeout)
    restic.init()
    atomic_write(cfg.password_file, blob)
    write_recipients(cfg, recipients)
    journal(cfg, "init", True, recipients=len(recipients))
    return {
        "identity_created": created_identity,
        "host_recipient": host,
        "recipients": len(recipients),
    }


def journal(cfg: Config, event: str, ok: bool, **details: Any) -> None:
    entry = {
        "time": datetime.now(UTC).isoformat(timespec="seconds"),
        "event": event,
        "ok": ok,
        **details,
    }
    cfg.vault.mkdir(parents=True, exist_ok=True)
    line = (json.dumps(entry, sort_keys=True) + "\n").encode()
    fd = os.open(cfg.journal_file, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        os.write(fd, line)  # one write: concurrent runs do not interleave inside a line
    finally:
        os.close(fd)


def read_journal(cfg: Config) -> list[dict[str, Any]]:
    try:
        text = cfg.journal_file.read_text(encoding="utf-8")
    except OSError:
        return []
    entries: list[dict[str, Any]] = []
    for line in text.splitlines():
        try:
            obj = json.loads(line)
        except ValueError:
            continue  # a torn or foreign line must not hide the rest
        if (
            isinstance(obj, dict)
            and isinstance(obj.get("time"), str)
            and isinstance(obj.get("event"), str)
        ):
            entries.append(obj)
    return entries


@contextmanager
def secret_file(content: str) -> Iterator[Path]:
    """A 0600 file in a 0700 directory, removed afterwards; for restic options that only take a file."""
    with tempfile.TemporaryDirectory(prefix="encbackup-") as directory:
        if os.name == "posix":
            os.chmod(directory, 0o700)
        path = Path(directory) / "secret"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="ascii") as handle:
            handle.write(content)
        yield path
