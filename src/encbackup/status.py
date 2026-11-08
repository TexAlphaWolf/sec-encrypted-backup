"""Status from the journal (no key needed), for monitoring: text, JSON or Prometheus."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from encbackup import vault
from encbackup.config import Config


def _parse(text: str) -> datetime | None:
    try:
        when = datetime.fromisoformat(text)
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=UTC)


def collect(cfg: Config, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    entries = vault.read_journal(cfg)

    def last(event: str, ok: bool | None = None) -> dict[str, Any] | None:
        for e in reversed(entries):
            if (
                e["event"] == event
                and (ok is None or bool(e.get("ok")) == ok)
                and _parse(e["time"])
            ):
                entry: dict[str, Any] = e
                return entry
        return None

    out: dict[str, Any] = {"problems": [], "warnings": []}
    for event in ("backup", "verify", "rotate"):
        e = last(event)
        out[event] = e
    good = last("backup", True)
    if good is None:
        out["problems"].append("no successful backup recorded")
    else:
        when = _parse(good["time"])
        assert when is not None
        age_h = (now - when).total_seconds() / 3600
        out["backup_age_hours"] = round(age_h, 2)
        if age_h > cfg.max_age_hours:
            out["problems"].append(
                f"last successful backup is {age_h:.1f} h old (limit {cfg.max_age_hours:g} h)"
            )
    if out["backup"] and not out["backup"].get("ok"):
        out["problems"].append("the most recent backup run failed or was incomplete")
    if out["verify"] is None:
        out["warnings"].append("the backup has never been verified by a restore")
    elif not out["verify"].get("ok"):
        out["problems"].append("the most recent verification failed")
    try:
        recipients = vault.read_recipients(cfg)
        out["recipients"] = len(recipients)
        if not any(role == "recovery" for role, _ in recipients):
            out["warnings"].append(
                "no recovery recipient: losing this machine's identity loses the backups"
            )
    except Exception as exc:
        out["problems"].append(str(exc))
    out["ok"] = not out["problems"]
    return out


def prometheus(info: dict[str, Any]) -> str:
    def ts(entry: dict[str, Any] | None) -> float:
        when = _parse(entry["time"]) if entry else None
        return when.timestamp() if when else 0.0

    lines = [
        "# TYPE encbackup_backup_timestamp_seconds gauge",
        f"encbackup_backup_timestamp_seconds {ts(info['backup']):.0f}",
        "# TYPE encbackup_backup_success gauge",
        f"encbackup_backup_success {int(bool(info['backup'] and info['backup'].get('ok')))}",
        "# TYPE encbackup_verify_timestamp_seconds gauge",
        f"encbackup_verify_timestamp_seconds {ts(info['verify']):.0f}",
        "# TYPE encbackup_verify_success gauge",
        f"encbackup_verify_success {int(bool(info['verify'] and info['verify'].get('ok')))}",
        "# TYPE encbackup_ok gauge",
        f"encbackup_ok {int(info['ok'])}",
    ]
    return "\n".join(lines) + "\n"
