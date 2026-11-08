from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from encbackup import age, backup, rotate, vault, verify
from encbackup.proc import ToolError
from encbackup.restic import Restic
from helpers import World, tools

pytestmark = tools


def restic_with(world: World, password: str) -> Restic:
    return Restic(world.cfg.repository, password, world.root / "cache-test")


def test_password_rotation(ready: World) -> None:
    backup.run(ready.cfg)
    old = vault.unlock(ready.cfg)
    out = rotate.run(ready.cfg)
    new = vault.unlock(ready.cfg)
    assert new != old and len(new) >= 60
    assert not restic_with(ready, old).can_unlock()
    assert restic_with(ready, new).can_unlock()
    assert out["old_key_id"] != out["new_key_id"] and len(out["removed_keys"]) == 1
    assert not out["identity_rotated"]
    assert len(vault.restic_for(ready.cfg).key_list()) == 1
    assert verify.run(
        ready.cfg, recovery_identity=ready.recovery
    ).ok  # data and recovery key still fine
    assert vault.read_journal(ready.cfg)[-1]["event"] == "rotate"


def test_rotation_with_a_new_identity(ready: World) -> None:
    backup.run(ready.cfg)
    old_identity_bytes = ready.cfg.identity.read_bytes()
    old_key = age.public_key(ready.cfg.identity)
    out = rotate.run(ready.cfg, new_identity=True)
    retired = Path(out["retired_identity"])
    assert (
        retired.read_bytes() == old_identity_bytes
        and ready.cfg.identity.read_bytes() != old_identity_bytes
    )
    assert out["host_recipient"] == age.public_key(ready.cfg.identity) != old_key
    recipients = vault.read_recipients(ready.cfg)
    assert [r for r, _ in recipients] == ["recovery", "host"] or {k for _, k in recipients} == {
        ready.recovery_key,
        out["host_recipient"],
    }
    assert old_key not in {k for _, k in recipients}
    # the retired identity cannot open the new password file; the new and the recovery ones can
    with pytest.raises(ToolError, match="could not decrypt"):
        age.decrypt(ready.cfg.password_file.read_bytes(), retired)
    password = vault.unlock(ready.cfg)
    assert vault.unlock(ready.cfg, ready.recovery) == password
    assert verify.run(ready.cfg, recovery_identity=ready.recovery).ok


def test_two_identity_rotations_do_not_overwrite_the_retired_file(ready: World) -> None:
    backup.run(ready.cfg)
    first = Path(rotate.run(ready.cfg, new_identity=True)["retired_identity"])
    second = Path(rotate.run(ready.cfg, new_identity=True)["retired_identity"])
    assert first != second and first.exists() and second.exists()


def test_an_interrupted_identity_rotation_blocks_a_new_one(ready: World) -> None:
    backup.run(ready.cfg)
    ready.cfg.identity.with_name("host.key.new").write_text("leftover")
    before = vault.unlock(ready.cfg)
    with pytest.raises(ToolError, match="interrupted rotation"):
        rotate.run(ready.cfg, new_identity=True)
    # the new restic key was added but the vault is untouched: the old password still works
    assert vault.unlock(ready.cfg) == before and restic_with(ready, before).can_unlock()


def test_a_crash_after_adding_the_key_is_repaired_by_the_next_rotation(ready: World) -> None:
    backup.run(ready.cfg)
    r = vault.restic_for(ready.cfg)
    with vault.secret_file(vault.new_password()) as f:
        r.add_key(f)  # what step 1 does, then "crash"
    assert len(r.key_list()) == 2
    out = rotate.run(ready.cfg)
    assert len(vault.restic_for(ready.cfg).key_list()) == 1 and len(out["removed_keys"]) == 2


def test_keep_other_keys(ready: World) -> None:
    backup.run(ready.cfg)
    old = vault.unlock(ready.cfg)
    out = rotate.run(ready.cfg, keep_other_keys=True)
    assert out["removed_keys"] == []
    assert restic_with(ready, old).can_unlock()  # the old key was kept on request
    assert len(vault.restic_for(ready.cfg).key_list()) == 2


def test_a_failed_rotation_changes_nothing_that_matters(
    ready: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    backup.run(ready.cfg)
    before_blob = ready.cfg.password_file.read_bytes()
    monkeypatch.setattr(
        age, "encrypt", lambda *_a, **_k: (_ for _ in ()).throw(ToolError("age exploded"))
    )
    with pytest.raises(ToolError, match="age exploded"):
        rotate.run(ready.cfg)
    assert ready.cfg.password_file.read_bytes() == before_blob
    assert verify.run(ready.cfg).ok  # the vault still opens the repository


def test_a_broken_roundtrip_writes_nothing(ready: World, monkeypatch: pytest.MonkeyPatch) -> None:
    backup.run(ready.cfg)
    before = ready.cfg.password_file.read_bytes()
    real_decrypt = age.decrypt
    monkeypatch.setattr(age, "decrypt", lambda blob, ident: real_decrypt(blob, ident) + b"x")
    # unlock() uses age.decrypt too, so make only the second call lie
    calls = {"n": 0}

    def flaky(blob: bytes, ident: Path) -> bytes:
        calls["n"] += 1
        return real_decrypt(blob, ident) + (b"x" if calls["n"] > 1 else b"")

    monkeypatch.setattr(age, "decrypt", flaky)
    with pytest.raises(ToolError, match="does not decrypt to itself"):
        rotate.run(ready.cfg)
    assert ready.cfg.password_file.read_bytes() == before


def test_rotation_needs_a_working_vault(world: World) -> None:
    with pytest.raises(ToolError, match="was 'init' run"):
        rotate.run(world.cfg)


def test_copy_of_the_vault_on_a_new_machine_restores_with_the_recovery_identity(
    ready: World, tmp_path: Path
) -> None:
    backup.run(ready.cfg)
    # a new machine: no host identity at all, only the vault files, the repository and the recovery key
    newbox = tmp_path / "newbox"
    shutil.copytree(ready.cfg.vault, newbox / "vault")
    shutil.copytree(ready.cfg.repository, newbox / "repo")
    (newbox / "encbackup.toml").write_text(
        f'repository = "repo"\nvault = "vault"\nidentity = "absent.key"\n[backup]\npaths = ["{ready.data.as_posix()}"]\n'
    )
    from encbackup.config import load

    cfg = load(newbox / "encbackup.toml")
    r = vault.restic_for(cfg, ready.recovery)
    target = tmp_path / "restored"
    target.mkdir()
    r.restore(r.latest()["id"], target)
    for original in ready.data.rglob("*"):
        if original.is_file():
            copy = target / str(original).lstrip("/")
            assert copy.read_bytes() == original.read_bytes()
    assert os.path.exists(newbox / "repo")
