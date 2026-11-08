from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from encbackup import status
from encbackup.config import Config, load

NOW = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)


def cfg_with(tmp_path: Path, entries: list[dict[str, object]], *, recovery: bool = True) -> Config:
    (tmp_path / "v").mkdir()
    lines = [json.dumps(e) for e in entries]
    (tmp_path / "v" / "journal.jsonl").write_text("\n".join(lines) + "\n")
    key = "age1" + "q" * 58
    rec = f"recovery {key}\n" if recovery else ""
    (tmp_path / "v" / "recipients.txt").write_text(f"host {key}\n{rec}")
    (tmp_path / "c.toml").write_text(
        f'repository = "r"\nvault = "v"\nidentity = "i"\n[backup]\npaths = ["{(tmp_path / "d").as_posix()}"]\n'
    )
    return load(tmp_path / "c.toml")


def at(hours_ago: float) -> str:
    return (NOW - timedelta(hours=hours_ago)).isoformat(timespec="seconds")


def test_healthy(tmp_path: Path) -> None:
    cfg = cfg_with(
        tmp_path,
        [
            {"time": at(3), "event": "backup", "ok": True},
            {"time": at(3), "event": "verify", "ok": True},
        ],
    )
    info = status.collect(cfg, NOW)
    assert (
        info["ok"]
        and info["problems"] == []
        and info["warnings"] == []
        and info["backup_age_hours"] == 3.0
    )


def test_nothing_recorded(tmp_path: Path) -> None:
    info = status.collect(cfg_with(tmp_path, []), NOW)
    assert not info["ok"] and "no successful backup recorded" in info["problems"]
    assert any("never been verified" in w for w in info["warnings"])


def test_stale_failed_and_unverified(tmp_path: Path) -> None:
    entries = [
        {"time": at(60), "event": "backup", "ok": True},
        {"time": at(2), "event": "backup", "ok": False},
        {"time": at(1), "event": "verify", "ok": False},
    ]
    info = status.collect(cfg_with(tmp_path, entries), NOW)
    assert not info["ok"]
    text = " | ".join(info["problems"])
    assert (
        "60.0 h old" in text
        and "most recent backup run failed" in text
        and "verification failed" in text
    )


def test_a_later_success_clears_an_earlier_failure(tmp_path: Path) -> None:
    entries = [
        {"time": at(5), "event": "backup", "ok": False},
        {"time": at(1), "event": "backup", "ok": True},
        {"time": at(1), "event": "verify", "ok": True},
    ]
    assert status.collect(cfg_with(tmp_path, entries), NOW)["ok"]


def test_no_recovery_recipient_is_a_warning(tmp_path: Path) -> None:
    entries = [
        {"time": at(1), "event": "backup", "ok": True},
        {"time": at(1), "event": "verify", "ok": True},
    ]
    info = status.collect(cfg_with(tmp_path, entries, recovery=False), NOW)
    assert info["ok"] and any("no recovery recipient" in w for w in info["warnings"])


def test_a_missing_vault_is_a_problem_not_a_crash(tmp_path: Path) -> None:
    (tmp_path / "c.toml").write_text(
        f'repository = "r"\nvault = "nothing"\nidentity = "i"\n[backup]\npaths = ["{(tmp_path / "d").as_posix()}"]\n'
    )
    info = status.collect(load(tmp_path / "c.toml"), NOW)
    assert not info["ok"] and any("init" in p for p in info["problems"])


def test_prometheus(tmp_path: Path) -> None:
    entries = [
        {"time": at(3), "event": "backup", "ok": True},
        {"time": at(3), "event": "verify", "ok": False},
    ]
    text = status.prometheus(status.collect(cfg_with(tmp_path, entries), NOW))
    values = dict(line.split() for line in text.splitlines() if not line.startswith("#"))
    assert values["encbackup_backup_success"] == "1" and values["encbackup_verify_success"] == "0"
    assert values["encbackup_ok"] == "0"
    assert int(values["encbackup_backup_timestamp_seconds"]) == int(
        (NOW - timedelta(hours=3)).timestamp()
    )


def test_prometheus_with_an_empty_journal(tmp_path: Path) -> None:
    text = status.prometheus(status.collect(cfg_with(tmp_path, []), NOW))
    assert "encbackup_backup_timestamp_seconds 0" in text and "encbackup_ok 0" in text


def test_naive_timestamps_are_read_as_utc(tmp_path: Path) -> None:
    entries = [
        {"time": "2026-11-08T11:00:00", "event": "backup", "ok": True},
        {"time": at(1), "event": "verify", "ok": True},
    ]
    assert status.collect(cfg_with(tmp_path, entries), NOW)["backup_age_hours"] == 1.0
