"""A thin, typed layer over the restic command line (JSON output where restic offers it)."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from encbackup import proc
from encbackup.proc import ToolError

MIN_VERSION = (0, 16, 0)
EXIT_NO_REPO = 10
EXIT_WRONG_PASSWORD = 12
EXIT_INCOMPLETE = 3


def version() -> tuple[int, ...]:
    out = proc.run([proc.find("restic"), "version"]).out.decode()
    m = re.search(r"restic (\d+)\.(\d+)\.(\d+)", out)
    if not m:
        raise ToolError(f"cannot read the restic version from: {out.strip()[:80]!r}")
    return tuple(int(x) for x in m.groups())


def require_version() -> tuple[int, ...]:
    v = version()
    if v < MIN_VERSION:
        need = ".".join(map(str, MIN_VERSION))
        raise ToolError(f"restic {need} or newer is required (found {'.'.join(map(str, v))})")
    return v


@dataclass(frozen=True)
class BackupSummary:
    snapshot_id: str
    files_new: int
    files_changed: int
    files_unmodified: int
    data_added: int
    total_files: int
    total_bytes: int
    duration: float
    incomplete: bool


@dataclass(frozen=True)
class Node:
    path: str
    size: int
    mtime: str


class Restic:
    def __init__(
        self, repository: str, password: str, cache_dir: Path, timeout: float = 3600.0
    ) -> None:
        self.repository = repository
        self.password = password
        self.cache_dir = cache_dir
        self.timeout = timeout

    def _env(self) -> dict[str, str]:
        return proc.clean_env(
            {"RESTIC_PASSWORD": self.password, "RESTIC_CACHE_DIR": str(self.cache_dir)}
        )

    def _run(self, args: list[str], *, ok: tuple[int, ...] = (0,)) -> proc.Result:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        argv = [proc.find("restic"), "-r", self.repository, *args]
        try:
            return proc.run(
                argv,
                env=self._env(),
                ok=ok,
                timeout=self.timeout,
                secrets=(self.password,),
                describe=f"restic {args[0]}",
            )
        except ToolError as exc:
            text = str(exc)
            code = re.search(r"\(exit (\d+)\)", text)
            n = int(code.group(1)) if code else 0
            # restic before 0.17 reports a wrong password only through the message, with exit 1
            if n == EXIT_WRONG_PASSWORD or "wrong password or no key found" in text:
                raise ToolError("the repository password was not accepted") from exc
            if n == EXIT_NO_REPO or "Is there a repository at the following location" in text:
                raise ToolError(
                    f"there is no restic repository at {proc.redact(self.repository)}"
                ) from exc
            raise

    def init(self) -> None:
        self._run(["init", "--quiet"])

    def can_unlock(self) -> bool:
        """True if the password opens the repository; False if the password is wrong."""
        try:
            self._run(["cat", "config"])
        except ToolError as exc:
            if "password was not accepted" in str(exc):
                return False
            raise
        return True

    def backup(
        self, paths: tuple[str, ...], exclude: tuple[str, ...], tags: tuple[str, ...], one_fs: bool
    ) -> BackupSummary:
        args = ["backup", "--json"]
        for pattern in exclude:
            args += ["--exclude", pattern]
        for tag in tags:
            args += ["--tag", tag]
        if one_fs:
            args.append("--one-file-system")
        res = self._run([*args, "--", *paths], ok=(0, EXIT_INCOMPLETE))
        summary: dict[str, Any] | None = None
        for line in res.out.decode("utf-8", "replace").splitlines():
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if isinstance(obj, dict) and obj.get("message_type") == "summary":
                summary = obj
        if summary is None:
            raise ToolError("restic backup finished but reported no summary")
        return BackupSummary(
            snapshot_id=str(summary["snapshot_id"]),
            files_new=int(summary["files_new"]),
            files_changed=int(summary["files_changed"]),
            files_unmodified=int(summary["files_unmodified"]),
            data_added=int(summary["data_added"]),
            total_files=int(summary["total_files_processed"]),
            total_bytes=int(summary["total_bytes_processed"]),
            duration=float(summary["total_duration"]),
            incomplete=res.code == EXIT_INCOMPLETE,
        )

    def snapshots(self) -> list[dict[str, Any]]:
        data = json.loads(self._run(["snapshots", "--json"]).out or b"[]")
        if not isinstance(data, list):
            raise ToolError("restic snapshots returned something unexpected")
        return sorted(data, key=lambda s: str(s.get("time", "")))

    def latest(self) -> dict[str, Any]:
        snaps = self.snapshots()
        if not snaps:
            raise ToolError("the repository has no snapshots")
        return snaps[-1]

    def files(self, snapshot: str) -> Iterator[Node]:
        for line in (
            self._run(["ls", "--json", snapshot]).out.decode("utf-8", "replace").splitlines()
        ):
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if (
                isinstance(obj, dict)
                and obj.get("type") == "file"
                and obj.get("struct_type") == "node"
            ):
                yield Node(str(obj["path"]), int(obj.get("size", 0)), str(obj.get("mtime", "")))

    def restore(self, snapshot: str, target: Path, includes: list[str] | None = None) -> None:
        args = ["restore", snapshot, "--target", str(target), "--verify", "--quiet"]
        for item in includes or []:
            args += ["--include", item]
        self._run(args)

    def check(self, subset: str | None) -> None:
        args = ["check"]
        if subset:
            args.append(f"--read-data-subset={subset}")
        self._run(args)

    def forget(
        self, *, last: int, daily: int, weekly: int, monthly: int, yearly: int, prune: bool
    ) -> None:
        args = ["forget", "--quiet"]
        for flag, n in (
            ("last", last),
            ("daily", daily),
            ("weekly", weekly),
            ("monthly", monthly),
            ("yearly", yearly),
        ):
            if n:
                args += [f"--keep-{flag}", str(n)]
        if prune:
            args.append("--prune")
        self._run(args)

    def key_list(self) -> list[dict[str, Any]]:
        data = json.loads(self._run(["key", "list", "--json"]).out or b"[]")
        if not isinstance(data, list):
            raise ToolError("restic key list returned something unexpected")
        return data

    def add_key(self, new_password_file: Path) -> None:
        self._run(["key", "add", "--new-password-file", str(new_password_file)])

    def remove_key(self, key_id: str) -> None:
        if not re.fullmatch(r"[0-9a-f]{8,64}", key_id):
            raise ToolError("unexpected key id from restic")
        self._run(["key", "remove", key_id])

    def stats_raw(self) -> int:
        out = json.loads(self._run(["stats", "--mode", "raw-data", "--json"]).out)
        return int(out["total_size"])
