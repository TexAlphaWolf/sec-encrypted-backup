from __future__ import annotations

from pathlib import Path

import pytest

from encbackup.config import load
from encbackup.proc import ToolError

BASE = 'repository = "r"\nvault = "v"\nidentity = "i.key"\n[backup]\npaths = ["@DATA@"]\n'


def cfg(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "c.toml"
    p.write_text(text.replace("@DATA@", (tmp_path / "data").as_posix()))
    return p


def test_minimal_and_defaults(tmp_path: Path) -> None:
    c = load(cfg(tmp_path, BASE))
    assert c.paths == ((tmp_path / "data").as_posix(),)
    assert c.data_subset == "5%" and c.sample_files == 200 and c.max_age_hours == 26
    assert not c.retention.enabled()
    assert c.password_file == c.vault / "repo.key.age"


def test_relative_paths_resolve_against_the_config_file(tmp_path: Path) -> None:
    c = load(cfg(tmp_path, BASE))
    assert c.vault == (tmp_path / "v").resolve()
    assert c.identity == (tmp_path / "i.key").resolve()
    assert c.repository == str((tmp_path / "r").resolve())


def test_remote_repositories_are_left_alone(tmp_path: Path) -> None:
    for spec in (
        "sftp:backup@host:/srv/restic",
        "s3:s3.amazonaws.com/bucket",
        "rest:https://h/repo/",
    ):
        c = load(cfg(tmp_path, BASE.replace('"r"', f'"{spec}"')))
        assert c.repository == spec


def test_everything_set(tmp_path: Path) -> None:
    text = BASE.replace(
        'paths = ["@DATA@"]',
        'paths = ["@DATA@", "@DATA@/b"]\nexclude = ["*.tmp"]\ntags = ["nightly"]\none_file_system = true',
    )
    text += '[retention]\nlast = 3\ndaily = 7\nprune = false\n[verify]\ndata_subset = "1/10"\nsample_files = 5\nmax_age_hours = 12.5\n'
    c = load(cfg(tmp_path, text))
    assert c.retention.enabled() and c.retention.daily == 7 and not c.retention.prune
    assert c.exclude == ("*.tmp",) and c.tags == ("nightly",) and c.one_file_system
    assert c.data_subset == "1/10" and c.max_age_hours == 12.5


@pytest.mark.parametrize(
    "text, message",
    [
        ("not toml [", "not valid TOML"),
        (BASE + "surprise = 1\n", "unknown key"),
        (BASE.replace('identity = "i.key"\n', ""), "'identity' is required"),
        (BASE.replace('paths = ["@DATA@"]', "paths = []"), "absolute"),
        (BASE.replace('paths = ["@DATA@"]', 'paths = ["relative"]'), "absolute"),
        (BASE.replace('paths = ["@DATA@"]', "paths = 3"), "list of non-empty strings"),
        (BASE.replace('[backup]\npaths = ["@DATA@"]\n', "[backup]\n"), "paths is required"),
        (BASE + "[backup2]\nx = 1\n", "unknown key"),
        (BASE + "[retention]\ndaily = -1\n", "whole number"),
        (BASE + "[retention]\ndaily = true\n", "whole number"),
        (BASE + "[retention]\nweekly = 1.5\n", "whole number"),
        (BASE + "[retention]\nprune = 1\n", "true or false"),
        (BASE + '[verify]\ndata_subset = "all of it"\n', "data_subset"),
        (BASE + "[verify]\nsample_files = 99999999\n", "sample_files"),
        (BASE + "[verify]\nmax_age_hours = 0\n", "max_age_hours"),
        (BASE + "[retention]\nunknown = 1\n", "unknown key"),
        (BASE.replace("[backup]", '[backup]\ntags = ["bad tag!"]'), "invalid entry"),
        (BASE.replace("[backup]", '[backup]\none_file_system = "yes"'), "one_file_system"),
        (
            'repository = "a\\nb"\nvault = "v"\nidentity = "i"\n[backup]\npaths = ["@DATA@"]\n',
            "control character",
        ),
        (BASE.replace("[backup]", "backup = 3\n[backup]"), "not valid TOML"),
    ],
)
def test_rejects(tmp_path: Path, text: str, message: str) -> None:
    with pytest.raises(ToolError, match=message):
        load(cfg(tmp_path, text))


def test_missing_and_huge_files(tmp_path: Path) -> None:
    with pytest.raises(ToolError, match="cannot read"):
        load(tmp_path / "absent.toml")
    big = tmp_path / "big.toml"
    big.write_text("# " + "x" * 1_100_000)
    with pytest.raises(ToolError, match="unreasonably large"):
        load(big)


def test_not_utf8(tmp_path: Path) -> None:
    p = tmp_path / "c.toml"
    p.write_bytes(b"\xff\xfe\x00")
    with pytest.raises(ToolError, match="not valid TOML"):
        load(p)


def test_a_table_where_a_value_is_expected(tmp_path: Path) -> None:
    with pytest.raises(ToolError, match="must be a table"):
        load(cfg(tmp_path, "retention = 3\n" + BASE))
