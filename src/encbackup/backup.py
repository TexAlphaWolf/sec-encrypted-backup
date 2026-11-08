"""Back up, then apply the retention policy."""

from __future__ import annotations

from typing import Any

from encbackup import vault
from encbackup.config import Config
from encbackup.proc import ToolError


def run(cfg: Config) -> dict[str, Any]:
    restic = vault.restic_for(cfg)
    try:
        s = restic.backup(cfg.paths, cfg.exclude, cfg.tags, cfg.one_file_system)
    except ToolError as exc:
        vault.journal(cfg, "backup", False, error=str(exc)[:400])
        raise
    result: dict[str, Any] = {
        "snapshot": s.snapshot_id[:8],
        "snapshot_id": s.snapshot_id,
        "files": s.total_files,
        "bytes": s.total_bytes,
        "files_new": s.files_new,
        "files_changed": s.files_changed,
        "added_bytes": s.data_added,
        "seconds": round(s.duration, 3),
        "incomplete": s.incomplete,
        "forgot": False,
    }
    vault.journal(
        cfg,
        "backup",
        not s.incomplete,
        snapshot=result["snapshot"],
        added_bytes=s.data_added,
        files=s.total_files,
        incomplete=s.incomplete,
    )
    keep = cfg.retention
    if keep.enabled():
        try:
            restic.forget(
                last=keep.last,
                daily=keep.daily,
                weekly=keep.weekly,
                monthly=keep.monthly,
                yearly=keep.yearly,
                prune=keep.prune,
            )
            result["forgot"] = True
        except ToolError as exc:
            vault.journal(cfg, "retention", False, error=str(exc)[:400])
            result["retention_error"] = str(exc)
    return result
