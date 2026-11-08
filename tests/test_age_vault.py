from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from encbackup import age, vault
from encbackup.proc import ToolError
from helpers import World, make_identity, tools

pytestmark = tools


def test_roundtrip_and_wrong_identity(tmp_path: Path) -> None:
    mine, other = tmp_path / "mine", tmp_path / "other"
    key = make_identity(mine)
    make_identity(other)
    blob = age.encrypt(b"s3cret-value", [key])
    assert b"s3cret-value" not in blob and blob.startswith(b"-----BEGIN AGE ENCRYPTED FILE-----")
    assert age.decrypt(blob, mine) == b"s3cret-value"
    with pytest.raises(ToolError, match="could not decrypt"):
        age.decrypt(blob, other)


def test_every_recipient_can_open(tmp_path: Path) -> None:
    ids = [tmp_path / f"id{i}" for i in range(3)]
    keys = [make_identity(i) for i in ids]
    blob = age.encrypt(b"shared", keys)
    assert all(age.decrypt(blob, i) == b"shared" for i in ids)


def test_recipient_validation() -> None:
    for bad in (
        "",
        "age1short",
        "AGE1" + "q" * 58,
        "age1" + "b" * 58,
        "age1" + "q" * 59,
        "ssh-ed25519 AAAA",
        "age1" + "q" * 58 + "\n-r x",
    ):
        with pytest.raises(ToolError, match="not an age recipient"):
            age.check_recipient(bad)
    with pytest.raises(ToolError, match="no recipients"):
        age.encrypt(b"x", [])


@pytest.mark.skipif(os.name != "posix", reason="POSIX permissions")
def test_identity_file_must_be_private(tmp_path: Path) -> None:
    ident = tmp_path / "id"
    key = make_identity(ident)
    blob = age.encrypt(b"x", [key])
    ident.chmod(0o640)
    with pytest.raises(ToolError, match="accessible to other users"):
        age.decrypt(blob, ident)
    ident.chmod(0o600)
    assert age.decrypt(blob, ident) == b"x"


def test_generated_identity_is_private_and_never_overwritten(tmp_path: Path) -> None:
    ident = tmp_path / "sub" / "id"
    key = age.generate_identity(ident)
    assert age.RECIPIENT.match(key) and age.public_key(ident) == key
    if os.name == "posix":
        assert stat.S_IMODE(ident.stat().st_mode) == 0o600
    with pytest.raises(ToolError, match="refusing to overwrite"):
        age.generate_identity(ident)


def test_missing_identity_file(tmp_path: Path) -> None:
    with pytest.raises(ToolError, match="cannot read the identity file"):
        age.decrypt(b"x", tmp_path / "nope")


def test_init_creates_everything_and_no_secret_is_in_clear(world: World) -> None:
    cfg = world.cfg
    info = vault.init(cfg, [world.recovery_key])
    assert info["identity_created"] and info["recipients"] == 2
    password = vault.unlock(cfg)
    assert len(password) >= 60
    for path in (
        p
        for p in world.root.rglob("*")
        if p.is_file() and "repo" not in p.parts[len(world.root.parts) :][:1]
    ):
        assert password.encode() not in path.read_bytes(), path
    assert cfg.password_file.read_text().startswith("-----BEGIN AGE ENCRYPTED FILE-----")
    roles = [r for r, _ in vault.read_recipients(cfg)]
    assert roles == ["host", "recovery"]


def test_the_recovery_identity_opens_the_same_password(ready: World) -> None:
    assert vault.unlock(ready.cfg, ready.recovery) == vault.unlock(ready.cfg)


def test_init_refuses_twice_without_recovery_or_with_the_same_key(world: World) -> None:
    cfg = world.cfg
    with pytest.raises(ToolError, match="at least one --recovery-recipient"):
        vault.init(cfg, [])
    with pytest.raises(ToolError, match="not an age recipient"):
        vault.init(cfg, ["nonsense"])
    assert not cfg.vault.exists() or not cfg.password_file.exists()
    vault.init(cfg, [world.recovery_key])
    with pytest.raises(ToolError, match="already initialised"):
        vault.init(cfg, [world.recovery_key])


def test_init_refuses_when_recovery_equals_host(world: World) -> None:
    cfg = world.cfg
    make_identity(cfg.identity)  # an existing host identity
    host_key = age.public_key(cfg.identity)
    with pytest.raises(ToolError, match="different key"):
        vault.init(cfg, [host_key])
    assert not cfg.password_file.exists()


def test_init_reuses_an_existing_identity(world: World) -> None:
    cfg = world.cfg
    make_identity(cfg.identity)
    before = cfg.identity.read_bytes()
    info = vault.init(cfg, [world.recovery_key])
    assert not info["identity_created"] and cfg.identity.read_bytes() == before


def test_unlock_without_init(world: World) -> None:
    with pytest.raises(ToolError, match="was 'init' run"):
        vault.unlock(world.cfg)


def test_recipients_file_errors(ready: World) -> None:
    cfg = ready.cfg
    cfg.recipients_file.write_text("host nonsense\n")
    with pytest.raises(ToolError, match="not an age recipient"):
        vault.read_recipients(cfg)
    cfg.recipients_file.write_text("someone " + ready.recovery_key + "\n")
    with pytest.raises(ToolError, match="expected 'host"):
        vault.read_recipients(cfg)
    cfg.recipients_file.write_text("recovery " + ready.recovery_key + "\n")
    with pytest.raises(ToolError, match="no host recipient"):
        vault.read_recipients(cfg)
    cfg.recipients_file.unlink()
    with pytest.raises(ToolError, match="was 'init' run"):
        vault.read_recipients(cfg)


def test_a_corrupt_password_file_is_an_error_not_a_crash(ready: World) -> None:
    cfg = ready.cfg
    cfg.password_file.write_bytes(b"garbage")
    with pytest.raises(ToolError, match="could not decrypt"):
        vault.unlock(cfg)


def test_atomic_write_leaves_the_old_file_on_failure(tmp_path: Path) -> None:
    target = tmp_path / "f"
    vault.atomic_write(target, b"old")

    class Boom(bytes):
        pass

    with pytest.raises(TypeError):
        vault.atomic_write(target, "not bytes")  # type: ignore[arg-type]
    assert target.read_bytes() == b"old"
    assert [p.name for p in tmp_path.iterdir()] == ["f"]
    if os.name == "posix":
        assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_journal_skips_torn_and_foreign_lines(ready: World) -> None:
    cfg = ready.cfg
    vault.journal(cfg, "backup", True, files=3)
    with cfg.journal_file.open("a") as f:
        f.write('{"time": "2026-01-01T00:00:00+00:00", "event"\n[1, 2]\n{"event": "x"}\n\n')
    vault.journal(cfg, "verify", False)
    events = [(e["event"], e["ok"]) for e in vault.read_journal(cfg)]
    assert events == [("init", True), ("backup", True), ("verify", False)]


def test_secret_file_is_private_and_removed() -> None:
    with vault.secret_file("pw") as path:
        assert path.read_text() == "pw"
        if os.name == "posix":
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
            assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert not path.exists() and not path.parent.exists()
