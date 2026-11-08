"""Configuration: one TOML file, validated strictly (an unknown key is an error, not ignored)."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from encbackup.proc import ToolError

SUBSET = re.compile(r"^(\d{1,3}(\.\d+)?%|\d+/\d+|\d+(\.\d+)?[kKmMgGtT])$")
TAG = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]+:")  # sftp:, s3:, rest:, rclone: ... (not "C:")


@dataclass(frozen=True)
class Retention:
    last: int = 0
    daily: int = 0
    weekly: int = 0
    monthly: int = 0
    yearly: int = 0
    prune: bool = True

    def enabled(self) -> bool:
        return any((self.last, self.daily, self.weekly, self.monthly, self.yearly))


@dataclass(frozen=True)
class Config:
    repository: str
    vault: Path
    identity: Path
    paths: tuple[str, ...]
    exclude: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    one_file_system: bool = False
    retention: Retention = field(default_factory=Retention)
    data_subset: str = "5%"
    sample_files: int = 200
    max_age_hours: float = 26.0
    timeout: float = 6 * 3600.0

    @property
    def password_file(self) -> Path:
        return self.vault / "repo.key.age"

    @property
    def recipients_file(self) -> Path:
        return self.vault / "recipients.txt"

    @property
    def journal_file(self) -> Path:
        return self.vault / "journal.jsonl"

    @property
    def cache_dir(self) -> Path:
        return self.vault / "cache"


def _table(data: dict[str, Any], key: str, allowed: set[str], where: str) -> dict[str, Any]:
    value = data.get(key, {})
    if not isinstance(value, dict):
        raise ToolError(f"{where}: [{key}] must be a table")
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ToolError(f"{where}: unknown key(s) in [{key}]: {', '.join(unknown)}")
    return value


def _strings(
    value: Any, name: str, where: str, *, pattern: re.Pattern[str] | None = None
) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
        raise ToolError(f"{where}: {name} must be a list of non-empty strings")
    if pattern and not all(pattern.match(v) for v in value):
        raise ToolError(f"{where}: {name} has an invalid entry")
    return tuple(value)


def _int(value: Any, name: str, where: str, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ToolError(f"{where}: {name} must be a whole number from {minimum} to {maximum}")
    return value


def load(path: Path) -> Config:
    where = str(path)
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ToolError(f"cannot read the configuration {path}: {exc.strerror}") from exc
    if len(raw) > 1_000_000:
        raise ToolError(f"{where}: the configuration is unreasonably large")
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise ToolError(f"{where}: not valid TOML: {exc}") from exc

    top = {"repository", "vault", "identity", "backup", "retention", "verify"}
    unknown = sorted(set(data) - top)
    if unknown:
        raise ToolError(f"{where}: unknown key(s): {', '.join(unknown)}")
    for key in ("repository", "vault", "identity"):
        if not isinstance(data.get(key), str) or not data[key]:
            raise ToolError(f"{where}: '{key}' is required and must be a string")
    base = path.resolve().parent

    repository = data["repository"]
    if "\n" in repository or "\x00" in repository:
        raise ToolError(f"{where}: repository contains a control character")
    if not SCHEME.match(repository):
        repository = (
            str((base / repository).resolve()) if not Path(repository).is_absolute() else repository
        )

    backup = _table(data, "backup", {"paths", "exclude", "tags", "one_file_system"}, where)
    if "paths" not in backup:
        raise ToolError(f"{where}: [backup] paths is required")
    paths = _strings(backup["paths"], "[backup] paths", where)
    if not paths or not all(Path(p).is_absolute() for p in paths):
        raise ToolError(f"{where}: [backup] paths must be a non-empty list of absolute paths")
    exclude = _strings(backup.get("exclude", []), "[backup] exclude", where)
    tags = _strings(backup.get("tags", []), "[backup] tags", where, pattern=TAG)
    one_fs = backup.get("one_file_system", False)
    if not isinstance(one_fs, bool):
        raise ToolError(f"{where}: [backup] one_file_system must be true or false")

    keep = _table(
        data, "retention", {"last", "daily", "weekly", "monthly", "yearly", "prune"}, where
    )
    prune = keep.get("prune", True)
    if not isinstance(prune, bool):
        raise ToolError(f"{where}: [retention] prune must be true or false")
    retention = Retention(
        **{
            k: _int(keep.get(k, 0), f"[retention] {k}", where, minimum=0, maximum=10_000)
            for k in ("last", "daily", "weekly", "monthly", "yearly")
        },
        prune=prune,
    )

    verify = _table(
        data, "verify", {"data_subset", "sample_files", "max_age_hours", "timeout_hours"}, where
    )
    subset = verify.get("data_subset", "5%")
    if not isinstance(subset, str) or not SUBSET.match(subset):
        raise ToolError(f"{where}: [verify] data_subset must look like '5%', '1/10' or '500M'")
    hours = verify.get("max_age_hours", 26)
    timeout = verify.get("timeout_hours", 6)
    for name, val in (("max_age_hours", hours), ("timeout_hours", timeout)):
        if isinstance(val, bool) or not isinstance(val, (int, float)) or not 0 < val <= 24 * 365:
            raise ToolError(f"{where}: [verify] {name} must be a positive number")

    return Config(
        repository=repository,
        vault=(base / data["vault"]).resolve()
        if not Path(data["vault"]).is_absolute()
        else Path(data["vault"]),
        identity=(base / data["identity"]).resolve()
        if not Path(data["identity"]).is_absolute()
        else Path(data["identity"]),
        paths=paths,
        exclude=exclude,
        tags=tags,
        one_file_system=one_fs,
        retention=retention,
        data_subset=subset,
        sample_files=_int(
            verify.get("sample_files", 200),
            "[verify] sample_files",
            where,
            minimum=0,
            maximum=10_000,
        ),
        max_age_hours=float(hours),
        timeout=float(timeout) * 3600.0,
    )
