from __future__ import annotations

import os
from pathlib import Path

import pytest

from encbackup import backup, vault, verify
from encbackup.proc import ToolError
from encbackup.restic import Restic
from helpers import World, make_identity, tools

pytestmark = tools


def test_backup_then_a_second_run_stores_almost_nothing(ready: World) -> None:
    first = backup.run(ready.cfg)
    assert first["files"] == 3 and first["files_new"] == 3 and first["added_bytes"] > 70_000
    second = backup.run(ready.cfg)
    assert second["files_new"] == 0 and second["files_changed"] == 0
    assert second["added_bytes"] < first["added_bytes"] / 20  # deduplicated: only metadata
    assert [e["event"] for e in vault.read_journal(ready.cfg)] == ["init", "backup", "backup"]


def test_changed_file_stores_only_what_changed(ready: World) -> None:
    backup.run(ready.cfg)
    big = ready.data / "big.bin"
    block = os.urandom(16_000_000)
    big.write_bytes(block)
    s1 = backup.run(ready.cfg)
    big.write_bytes(block + os.urandom(1000))
    s2 = backup.run(ready.cfg)
    # content-defined chunking: only the last chunk (about 1 MiB on average) is stored again
    assert s2["files_changed"] == 1 and s2["added_bytes"] < 8_000_000 < s1["added_bytes"]


def test_retention_keeps_the_newest(world: World) -> None:
    world.write_config(retention="[retention]\nlast = 2\n")
    cfg = world.init()
    for i in range(4):
        (world.data / "a.txt").write_text(f"change {i}\n")
        out = backup.run(cfg)
    assert out["forgot"]
    assert len(vault.restic_for(cfg).snapshots()) == 2


def test_backup_failure_is_journaled_and_raised(ready: World) -> None:
    ready.write_config(backup='[backup]\npaths = ["/nonexistent-dir-xyz"]\n')
    with pytest.raises(ToolError):
        backup.run(ready.cfg)
    last = vault.read_journal(ready.cfg)[-1]
    assert last["event"] == "backup" and last["ok"] is False


@pytest.mark.skipif(os.name != "posix" or os.geteuid() == 0, reason="needs a non-root POSIX user")
def test_unreadable_file_gives_an_incomplete_snapshot(ready: World) -> None:
    secret = ready.data / "locked.txt"
    secret.write_text("x")
    secret.chmod(0)
    try:
        out = backup.run(ready.cfg)
    finally:
        secret.chmod(0o600)
    assert out["incomplete"] is True
    assert vault.read_journal(ready.cfg)[-1]["ok"] is False


def test_excludes_and_tags(world: World) -> None:
    world.write_config(
        backup=f'[backup]\npaths = ["{world.data.as_posix()}"]\nexclude = ["*.bin"]\ntags = ["nightly"]\n'
    )
    cfg = world.init()
    backup.run(cfg)
    r = vault.restic_for(cfg)
    names = [n.path for n in r.files(r.latest()["id"])]
    assert names and all(not n.endswith(".bin") for n in names)
    assert r.latest()["tags"] == ["nightly"]


# ---- verify -----------------------------------------------------------------------------------


def packs(cfg_repo: str) -> list[Path]:
    return sorted(p for p in (Path(cfg_repo) / "data").rglob("*") if p.is_file())


def test_verify_ok(ready: World) -> None:
    backup.run(ready.cfg)
    rep = verify.run(ready.cfg, recovery_identity=ready.recovery)
    assert rep.ok and rep.compared == 3 and rep.sampled == 3 and rep.bytes_compared > 70_000
    assert [c.name for c in rep.checks] == ["recovery key", "repository check", "restore"]
    assert rep.as_dict()["ok"] is True


def test_files_changed_or_deleted_after_the_snapshot_are_not_failures(ready: World) -> None:
    backup.run(ready.cfg)
    (ready.data / "a.txt").write_text("edited later\n")
    (ready.data / "sub" / "c.bin").unlink()
    rep = verify.run(ready.cfg)
    assert rep.ok and rep.changed_since == 1 and rep.gone_since == 1 and rep.compared == 1


def test_nothing_comparable_is_reported_as_failure(ready: World) -> None:
    backup.run(ready.cfg)
    for p in ready.data.rglob("*"):
        if p.is_file():
            p.write_text("different now\n")
    rep = verify.run(ready.cfg)
    assert not rep.ok and "no file could be compared" in rep.checks[-1].detail


def test_corrupted_data_is_caught_by_a_full_read(ready: World) -> None:
    backup.run(ready.cfg)
    victim = max(packs(ready.cfg.repository), key=lambda p: p.stat().st_size)
    raw = bytearray(victim.read_bytes())
    raw[len(raw) // 2] ^= 0xFF
    victim.write_bytes(bytes(raw))
    rep = verify.run(ready.cfg, full=True)
    assert not rep.ok
    assert any(c.name == "repository check" and not c.ok for c in rep.checks)


def test_a_restore_that_cannot_be_read_back_fails(ready: World) -> None:
    backup.run(ready.cfg)
    for p in packs(ready.cfg.repository):
        raw = bytearray(p.read_bytes())
        for i in range(0, len(raw), 997):
            raw[i] ^= 0xA5
        p.write_bytes(bytes(raw))
    rep = verify.run(ready.cfg, subset="0%" if False else None)
    assert not rep.ok


def test_a_truncated_pack_is_caught(ready: World) -> None:
    backup.run(ready.cfg)
    victim = packs(ready.cfg.repository)[0]
    victim.write_bytes(victim.read_bytes()[:100])
    assert not verify.run(ready.cfg, full=True).ok


def test_wrong_recovery_identity_and_a_stranger(ready: World, tmp_path: Path) -> None:
    backup.run(ready.cfg)
    stranger = tmp_path / "stranger"
    make_identity(stranger)
    rep = verify.run(ready.cfg, recovery_identity=stranger)
    assert not rep.ok and rep.checks[0].name == "recovery key" and not rep.checks[0].ok


def test_unknown_snapshot_and_empty_repository(ready: World) -> None:
    rep = verify.run(ready.cfg)
    assert not rep.ok and "no snapshots" in rep.checks[-1].detail
    backup.run(ready.cfg)
    rep = verify.run(ready.cfg, snapshot="deadbeef")
    assert not rep.ok and "no snapshot matches" in rep.checks[-1].detail


def test_a_specific_snapshot_by_prefix(ready: World) -> None:
    first = backup.run(ready.cfg)
    (ready.data / "a.txt").write_text("v2\n")
    backup.run(ready.cfg)
    rep = verify.run(ready.cfg, snapshot=first["snapshot"])
    assert rep.ok and rep.snapshot == first["snapshot"]
    assert rep.changed_since == 1  # a.txt was edited since


def test_sampling_is_deterministic_and_bounded(ready: World) -> None:
    for i in range(30):
        (ready.data / f"f{i}.txt").write_text(f"file {i}\n" * 10)
    backup.run(ready.cfg)
    a = verify.run(ready.cfg, sample=7)
    b = verify.run(ready.cfg, sample=7)
    c = verify.run(ready.cfg, sample=7, seed="another")
    assert a.sampled == b.sampled == c.sampled == 7 and a.ok and c.ok
    everything = verify.run(ready.cfg, sample=0)
    assert everything.sampled == everything.files_in_snapshot == 33


def test_files_with_pattern_characters_are_counted_not_restored(ready: World) -> None:
    (ready.data / "odd[1].txt").write_text("brackets\n")
    backup.run(ready.cfg)
    rep = verify.run(ready.cfg)
    assert rep.ok and rep.skipped_pattern == 1 and rep.sampled == 3


def test_more_files_than_one_restore_batch(ready: World) -> None:
    for i in range(verify.BATCH + 20):
        (ready.data / f"n{i}.txt").write_text(f"{i}\n")
    backup.run(ready.cfg)
    rep = verify.run(ready.cfg, sample=0)
    assert rep.ok and rep.compared == rep.sampled > verify.BATCH


def test_wrong_repository_and_wrong_password(ready: World, tmp_path: Path) -> None:
    backup.run(ready.cfg)
    r = Restic(ready.cfg.repository, "not the password", tmp_path / "cache")
    assert r.can_unlock() is False
    missing = Restic(str(tmp_path / "no-repo"), "x", tmp_path / "cache")
    with pytest.raises(ToolError, match="no restic repository"):
        missing.can_unlock()


def test_remote_credentials_never_reach_an_error_message(tmp_path: Path) -> None:
    r = Restic("rest:http://user:hunter2@127.0.0.1:1/repo", "pw", tmp_path / "cache", timeout=30)
    with pytest.raises(ToolError) as info:
        r.snapshots()
    assert "hunter2" not in str(info.value)
