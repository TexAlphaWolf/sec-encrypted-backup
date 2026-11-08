from __future__ import annotations

import json
from pathlib import Path

import pytest

from encbackup import cli, vault
from helpers import World, tools


def run(world: World, *args: str) -> int:
    return cli.main(["-c", str(world.root / "encbackup.toml"), *args])


@tools
def test_a_full_session(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    assert run(world, "init", "--recovery-recipient", world.recovery_key) == 0
    assert run(world, "backup", "--verify") == 0
    out = capsys.readouterr().out
    assert "snapshot" in out and "result: OK" in out
    assert run(world, "verify", "--recovery-identity", str(world.recovery), "--json") == 0
    report = json.loads(capsys.readouterr().out)
    assert report["ok"] and report["compared"] == 3
    assert run(world, "rotate", "--new-identity") == 0
    assert run(world, "status") == 0
    assert "result: OK" in capsys.readouterr().out
    target = world.root / "restored"
    assert run(world, "restore", "--target", str(target)) == 0
    restored = target / str(world.data / "a.txt").lstrip("/")
    assert restored.read_bytes() == (world.data / "a.txt").read_bytes()


@tools
def test_restore_refuses_a_non_empty_target_and_supports_include(
    ready: World, capsys: pytest.CaptureFixture[str]
) -> None:
    run(ready, "backup")
    target = ready.root / "full"
    target.mkdir()
    (target / "keep").write_text("mine")
    assert run(ready, "restore", "--target", str(target)) == 2
    assert (
        "not an empty directory" in capsys.readouterr().err
        and (target / "keep").read_text() == "mine"
    )
    only = ready.root / "only"
    assert run(ready, "restore", "--target", str(only), "--include", str(ready.data / "a.txt")) == 0
    files = [p for p in only.rglob("*") if p.is_file()]
    assert [p.name for p in files] == ["a.txt"]


@tools
def test_restore_with_the_recovery_identity_when_the_host_key_is_lost(ready: World) -> None:
    run(ready, "backup")
    ready.cfg.identity.unlink()
    target = ready.root / "r"
    assert run(ready, "restore", "--target", str(target)) == 2
    assert run(ready, "restore", "--target", str(target), "--identity", str(ready.recovery)) == 0


@tools
def test_restore_unknown_snapshot(ready: World, capsys: pytest.CaptureFixture[str]) -> None:
    run(ready, "backup")
    assert run(ready, "restore", "--target", str(ready.root / "t"), "--snapshot", "ffffffff") == 2
    assert "no snapshot matches" in capsys.readouterr().err


@tools
def test_exit_codes_for_verify_and_status_failures(
    ready: World, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(ready, "status") == 1  # nothing backed up yet
    assert run(ready, "verify") == 1  # nothing to restore
    out = capsys.readouterr().out
    assert "result: NOT OK" in out and "result: FAILED" in out
    run(ready, "backup")
    capsys.readouterr()
    # backed up but never verified is only a warning, but the failed verification above is recorded
    assert run(ready, "status", "--format", "json") == 1
    info = json.loads(capsys.readouterr().out)
    assert info["problems"] == ["the most recent verification failed"]
    assert run(ready, "verify") == 0
    assert run(ready, "status", "--format", "prometheus") == 0
    assert "encbackup_ok 1" in capsys.readouterr().out


@tools
def test_secrets_never_appear_in_output(ready: World, capsys: pytest.CaptureFixture[str]) -> None:
    password = vault.unlock(ready.cfg)
    identity = ready.cfg.identity.read_text()
    secret_line = next(line for line in identity.splitlines() if line.startswith("AGE-SECRET-KEY-"))
    for args in (
        ["backup", "--json"],
        ["verify", "--json"],
        ["status", "--format", "json"],
        ["rotate"],
    ):
        run(ready, *args)
    seen = capsys.readouterr()
    assert password not in seen.out + seen.err and secret_line not in seen.out + seen.err
    assert vault.unlock(ready.cfg) not in seen.out + seen.err  # the new one, after the rotation


@tools
def test_ambient_restic_variables_cannot_redirect_a_run(
    ready: World, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("RESTIC_REPOSITORY", str(tmp_path / "somewhere-else"))
    monkeypatch.setenv("RESTIC_PASSWORD_FILE", str(tmp_path / "wrong"))
    monkeypatch.setenv("RESTIC_PASSWORD", "wrong")
    assert run(ready, "backup") == 0
    assert not (tmp_path / "somewhere-else").exists()


@tools
def test_init_errors_are_reported_not_raised(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(world, "init") == 2
    assert "--recovery-recipient" in capsys.readouterr().err
    assert run(world, "init", "--recovery-recipient", "nonsense") == 2


def test_bad_configuration_and_missing_programs(
    world: World, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    assert cli.main(["-c", str(world.root / "absent.toml"), "status"]) == 2
    assert "cannot read the configuration" in capsys.readouterr().err
    monkeypatch.setenv("ENCBACKUP_RESTIC", "/no/such/restic")
    assert run(world, "backup") == 2
    assert "cannot run restic" in capsys.readouterr().err


def test_old_restic_is_refused(
    monkeypatch: pytest.MonkeyPatch, world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    from encbackup import restic

    monkeypatch.setattr(restic, "version", lambda: (0, 14, 0))
    assert run(world, "backup") == 2
    assert "0.16.0 or newer" in capsys.readouterr().err


def test_human_sizes() -> None:
    assert [cli.human(n) for n in (0, 1023, 1024, 5 * 1024**2, 3 * 1024**4)] == [
        "0 B",
        "1023 B",
        "1.0 KiB",
        "5.0 MiB",
        "3.0 TiB",
    ]
    assert cli.human(10 * 1024**5).endswith("TiB")


def test_the_parser_requires_a_command_and_knows_the_version(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit):
        cli.main([])
    with pytest.raises(SystemExit) as info:
        cli.main(["--version"])
    assert info.value.code == 0 and "encbackup 1.0.0" in capsys.readouterr().out
