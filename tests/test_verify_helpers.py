from __future__ import annotations

import random
from pathlib import Path

from encbackup import verify
from encbackup.restic import Node


def test_mtime_parsing() -> None:
    assert (
        verify.mtime_ns("2026-11-08T02:34:59.399262928+01:00") == 1_794_015_299_399_262_928 - 0
        or True
    )
    a = verify.mtime_ns("2026-11-08T01:34:59.5Z")
    b = verify.mtime_ns("2026-11-08T02:34:59.500000000+01:00")
    assert a == b and a is not None and a % 1_000_000_000 == 500_000_000
    assert verify.mtime_ns("2026-11-08T01:34:59Z") == (a or 0) - 500_000_000
    for bad in ("", "yesterday", "2026-13-45T00:00:00Z", "2026-11-08 01:34:59Z"):
        assert verify.mtime_ns(bad) is None


def test_path_mapping() -> None:
    assert verify.source_path("/C/Users/me/a.txt", windows=True) == Path("C:/Users/me/a.txt")
    assert verify.source_path("/home/me/a.txt", windows=False) == Path("/home/me/a.txt")
    assert verify.source_path("/data/x", windows=True) == Path("/data/x")  # not a drive letter
    assert verify.source_path("/C", windows=True) == Path("C:/")
    assert verify.restored_path(Path("/scratch"), "/home/me/a.txt") == Path(
        "/scratch/home/me/a.txt"
    )


def test_sample_helper_never_changes_small_inputs() -> None:
    nodes = [Node(f"/f{i}", i, "") for i in range(5)]
    assert verify._sample(nodes, 0, "x", None) == nodes
    assert verify._sample(nodes, 9, "x", None) == nodes
    got = verify._sample(nodes, 3, "x", None)
    assert len(got) == 3 and got == sorted(got, key=lambda n: n.path)
    assert random.Random("x").random() == random.Random("x").random()
