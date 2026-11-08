"""Running restic and age: no shell, a timeout, and nothing secret in an error message."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from collections.abc import Iterable, Mapping
from dataclasses import dataclass


class ToolError(Exception):
    """A problem with restic, age or the environment around them. The message is safe to print."""


# Ambient settings that would silently redirect restic to another repository or password.
_AMBIENT = (
    "RESTIC_PASSWORD",
    "RESTIC_PASSWORD_FILE",
    "RESTIC_PASSWORD_COMMAND",
    "RESTIC_REPOSITORY",
    "RESTIC_REPOSITORY_FILE",
    "RESTIC_FROM_PASSWORD",
    "RESTIC_FROM_PASSWORD_FILE",
    "RESTIC_FROM_PASSWORD_COMMAND",
    "RESTIC_FROM_REPOSITORY",
    "RESTIC_FROM_REPOSITORY_FILE",
)

_URL_CREDENTIALS = re.compile(r"(?<=://)[^/\s:@]+:[^@\s/]+@")
_AGE_SECRET = re.compile(r"AGE-SECRET-KEY-[A-Z0-9]+")


def redact(text: str, secrets: Iterable[str] = ()) -> str:
    """Remove URL credentials, age secret keys and any given secret from text."""
    text = _URL_CREDENTIALS.sub("***:***@", text)
    text = _AGE_SECRET.sub("[age secret key removed]", text)
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[secret removed]")
    return text


def find(tool: str) -> str:
    """Path of restic or age; ENCBACKUP_RESTIC / ENCBACKUP_AGE override the search."""
    override = os.environ.get(f"ENCBACKUP_{tool.upper().replace('-', '_')}")
    found = override or shutil.which(tool)
    if not found:
        raise ToolError(
            f"{tool} was not found in PATH (install it, or set ENCBACKUP_{tool.upper().replace('-', '_')})"
        )
    return found


def clean_env(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in _AMBIENT}
    if extra:
        env.update(extra)
    return env


@dataclass(frozen=True)
class Result:
    code: int
    out: bytes
    err: str


def run(
    argv: list[str],
    *,
    env: Mapping[str, str] | None = None,
    stdin: bytes | None = None,
    timeout: float = 3600.0,
    ok: tuple[int, ...] = (0,),
    secrets: Iterable[str] = (),
    describe: str | None = None,
) -> Result:
    """Run a command. Raises ToolError with a redacted message unless the exit code is in ok."""
    name = describe or os.path.basename(argv[0])
    secret_list = list(secrets)
    try:
        done = subprocess.run(
            argv,
            input=stdin,
            capture_output=True,
            timeout=timeout,
            env=dict(env) if env is not None else None,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ToolError(f"{name} did not finish within {timeout:.0f} s") from exc
    except OSError as exc:
        raise ToolError(f"cannot run {name}: {exc.strerror or exc}") from exc
    err = done.stderr.decode("utf-8", "replace")
    if done.returncode not in ok:
        tail = redact(err.strip(), secret_list)
        tail = tail[-800:] if len(tail) > 800 else tail
        raise ToolError(f"{name} failed (exit {done.returncode}): {tail or 'no message'}")
    return Result(done.returncode, done.stdout, redact(err, secret_list))
