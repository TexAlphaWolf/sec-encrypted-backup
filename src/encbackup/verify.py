"""Verification by restoring: prove the backup can be read back, not only that it exists.

Steps, each reported on its own:
  1. the recovery identity (if given) opens the same password as the host's;
  2. `restic check`, reading a subset of the stored data (or all of it);
  3. a sample of files is restored from the snapshot into a scratch directory, restic checks
     every restored file against its own hashes, and each file is compared with the live source
     by SHA-256 when the source has not changed since the snapshot.
"""

from __future__ import annotations

import hashlib
import os
import random
import re
import tempfile
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any

from encbackup import vault
from encbackup.config import Config
from encbackup.proc import ToolError
from encbackup.restic import Node, Restic

BATCH = 100
_PATTERN_CHARS = set("*?[]\\")
_TIME = re.compile(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d{1,9}))?(Z|[+-]\d\d:\d\d)$")


@dataclass
class Check:
    name: str
    ok: bool
    detail: str


@dataclass
class Report:
    snapshot: str = ""
    checks: list[Check] = field(default_factory=list)
    files_in_snapshot: int = 0
    sampled: int = 0
    compared: int = 0
    changed_since: int = 0
    gone_since: int = 0
    skipped_pattern: int = 0
    bytes_compared: int = 0
    seconds: float = 0.0

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.checks)

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["ok"] = self.ok
        return d


def _line(exc: Exception) -> str:
    """One line, bounded: restic prints several."""
    text = " ".join(str(exc).split())
    return text if len(text) <= 230 else text[:227] + "..."


def mtime_ns(text: str) -> int | None:
    """restic prints times as RFC 3339 with nanoseconds; return nanoseconds since the epoch."""
    m = _TIME.match(text)
    if not m:
        return None
    base, frac, zone = m.groups()
    try:
        seconds = int(
            datetime.fromisoformat(base + ("+00:00" if zone == "Z" else zone)).timestamp()
        )
    except ValueError:
        return None
    return seconds * 1_000_000_000 + int((frac or "0").ljust(9, "0"))


def source_path(restic_path: str, *, windows: bool = os.name == "nt") -> Path:
    """The path on this machine for a path as restic lists it ('/C/Users/x' is C:\\Users\\x)."""
    if windows:
        m = re.match(r"^/([A-Za-z])(/.*)?$", restic_path)
        if m:
            return Path(f"{m.group(1)}:{m.group(2) or '/'}")
    return Path(restic_path)


def restored_path(target: Path, restic_path: str) -> Path:
    parts = PurePosixPath(restic_path).parts
    return target.joinpath(*[p for p in parts if p != "/"])


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _sample(nodes: list[Node], count: int, snapshot_id: str, seed: str | None) -> list[Node]:
    if count <= 0 or len(nodes) <= count:
        return nodes
    rng = random.Random(seed if seed is not None else snapshot_id)
    return sorted(rng.sample(nodes, count), key=lambda n: n.path)


def run(
    cfg: Config,
    *,
    snapshot: str = "latest",
    recovery_identity: Path | None = None,
    subset: str | None = None,
    sample: int | None = None,
    seed: str | None = None,
    full: bool = False,
) -> Report:
    started = time.monotonic()
    report = Report()
    restic = vault.restic_for(cfg)

    if recovery_identity is not None:
        try:
            ok = vault.unlock(cfg, recovery_identity) == restic.password
            report.checks.append(
                Check(
                    "recovery key",
                    ok,
                    "opens the repository password" if ok else "decrypts to a different password",
                )
            )
        except ToolError as exc:
            report.checks.append(Check("recovery key", False, _line(exc)))

    chosen_subset = "100%" if full else (subset if subset is not None else cfg.data_subset)
    try:
        restic.check(chosen_subset or None)
        report.checks.append(
            Check(
                "repository check",
                True,
                f"structure and {chosen_subset or 'no'} of the data read back",
            )
        )
    except ToolError as exc:
        report.checks.append(Check("repository check", False, _line(exc)))

    try:
        snap = (
            restic.latest()
            if snapshot == "latest"
            else next(
                (s for s in restic.snapshots() if str(s.get("id", "")).startswith(snapshot)), None
            )
        )
        if snap is None:
            raise ToolError(f"no snapshot matches {snapshot!r}")
        snap_id = str(snap["id"])
        report.snapshot = snap_id[:8]
        nodes = list(restic.files(snap_id))
    except ToolError as exc:
        report.checks.append(Check("restore", False, _line(exc)))
        report.seconds = round(time.monotonic() - started, 3)
        return report

    report.files_in_snapshot = len(nodes)
    usable = [n for n in nodes if not (_PATTERN_CHARS & set(n.path))]
    report.skipped_pattern = len(nodes) - len(usable)
    wanted = _sample(usable, cfg.sample_files if sample is None else sample, snap_id, seed)
    report.sampled = len(wanted)
    report.checks.append(_restore_and_compare(restic, snap_id, wanted, report))
    report.seconds = round(time.monotonic() - started, 3)
    return report


def _restore_and_compare(restic: Restic, snap_id: str, wanted: list[Node], report: Report) -> Check:
    problems: list[str] = []
    with tempfile.TemporaryDirectory(prefix="encbackup-restore-") as scratch:
        target = Path(scratch)
        try:
            for i in range(0, len(wanted), BATCH):
                restic.restore(snap_id, target, [n.path for n in wanted[i : i + BATCH]])
        except ToolError as exc:
            return Check("restore", False, _line(exc))
        for node in wanted:
            back = restored_path(target, node.path)
            if not back.is_file():
                problems.append(f"{node.path}: missing after restore")
                continue
            if back.stat().st_size != node.size:
                problems.append(
                    f"{node.path}: restored {back.stat().st_size} bytes, snapshot says {node.size}"
                )
                continue
            live = source_path(node.path)
            try:
                st = live.stat()
            except OSError:
                report.gone_since += 1
                continue
            if st.st_size != node.size or mtime_ns(node.mtime) != st.st_mtime_ns:
                report.changed_since += 1
                continue
            if sha256(back) != sha256(live):
                problems.append(f"{node.path}: restored content differs from the source")
                continue
            report.compared += 1
            report.bytes_compared += node.size
    if problems:
        shown = "; ".join(problems[:5]) + (
            f" (+{len(problems) - 5} more)" if len(problems) > 5 else ""
        )
        return Check("restore", False, shown)
    if report.sampled and report.compared == 0:
        return Check(
            "restore",
            False,
            "restored fine, but no file could be compared with its source (all changed or gone since the snapshot)",
        )
    return Check(
        "restore",
        True,
        f"{report.sampled} files restored, {report.compared} identical to the source",
    )
