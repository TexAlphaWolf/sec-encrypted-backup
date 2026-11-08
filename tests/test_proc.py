from __future__ import annotations

import sys

import pytest

from encbackup import proc


def test_redact_url_credentials_and_age_keys() -> None:
    text = "open rest:https://alice:s3cret@host/repo failed; key AGE-SECRET-KEY-1QQQ234ABC"
    out = proc.redact(text)
    assert "s3cret" not in out and "alice" not in out and "AGE-SECRET-KEY-1QQQ" not in out
    assert "https://***:***@host/repo" in out


def test_redact_given_secrets_and_no_credentials_untouched() -> None:
    assert (
        proc.redact("pw=hunter2 at sftp:me@host:/x", ["hunter2"])
        == "pw=[secret removed] at sftp:me@host:/x"
    )
    assert proc.redact("nothing here", [""]) == "nothing here"


def test_failure_message_is_redacted_and_short() -> None:
    code = (
        "import sys; sys.stderr.write('user:pw@x ' + 'https://u:p4ss@h/ ' + 'z'*5000); sys.exit(7)"
    )
    with pytest.raises(proc.ToolError) as info:
        proc.run([sys.executable, "-c", code], secrets=["z" * 10])
    message = str(info.value)
    assert "exit 7" in message and "p4ss" not in message and len(message) < 1000


def test_ok_codes_and_output() -> None:
    res = proc.run([sys.executable, "-c", "print('x'); raise SystemExit(3)"], ok=(0, 3))
    assert res.code == 3 and res.out.strip() == b"x"


def test_timeout() -> None:
    with pytest.raises(proc.ToolError, match="did not finish"):
        proc.run([sys.executable, "-c", "import time; time.sleep(30)"], timeout=0.3)


def test_missing_program() -> None:
    with pytest.raises(proc.ToolError, match="cannot run"):
        proc.run(["/definitely/not/here"])


def test_find_missing_and_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENCBACKUP_NOSUCH", "/x/y")
    assert proc.find("nosuch") == "/x/y"
    monkeypatch.delenv("ENCBACKUP_NOSUCH")
    with pytest.raises(proc.ToolError, match="not found in PATH"):
        proc.find("nosuch-program-xyz")


def test_clean_env_drops_ambient_restic_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RESTIC_PASSWORD_FILE", "/tmp/elsewhere")
    monkeypatch.setenv("RESTIC_REPOSITORY", "/tmp/other")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "keep-me")
    env = proc.clean_env({"RESTIC_PASSWORD": "x"})
    assert "RESTIC_PASSWORD_FILE" not in env and "RESTIC_REPOSITORY" not in env
    assert env["AWS_ACCESS_KEY_ID"] == "keep-me" and env["RESTIC_PASSWORD"] == "x"
