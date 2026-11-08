from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from helpers import World, make_identity

HAVE_TOOLS = all(shutil.which(t) for t in ("restic", "age", "age-keygen"))
REQUIRED = os.environ.get("ENCBACKUP_REQUIRE_TOOLS") == "1"


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if HAVE_TOOLS:
        return
    if REQUIRED:
        raise pytest.UsageError(
            "ENCBACKUP_REQUIRE_TOOLS=1 but restic, age or age-keygen is missing"
        )
    skip = pytest.mark.skip(reason="restic and age are not installed")
    for item in items:
        if "tools" in item.keywords:
            item.add_marker(skip)


@pytest.fixture
def world(tmp_path: Path) -> World:
    data = tmp_path / "data"
    (data / "sub").mkdir(parents=True)
    (data / "a.txt").write_text("alpha\n" * 100)
    (data / "sub" / "b.bin").write_bytes(os.urandom(50_000))
    (data / "sub" / "c.bin").write_bytes(os.urandom(20_000))
    recovery = tmp_path / "recovery.txt"
    key = make_identity(recovery)

    def write_config(**extra: str) -> Path:
        body = {
            "head": 'repository = "repo"\nvault = "vault"\nidentity = "host.key"\n',
            "backup": f'[backup]\npaths = ["{data.as_posix()}"]\n',
            "verify": "[verify]\nsample_files = 50\n",
        }
        body.update(extra)
        path = tmp_path / "encbackup.toml"
        path.write_text("\n".join(body.values()))
        return path

    write_config()
    return World(tmp_path, data, recovery, key, write_config)


@pytest.fixture
def ready(world: World) -> World:
    world.init()
    return world
