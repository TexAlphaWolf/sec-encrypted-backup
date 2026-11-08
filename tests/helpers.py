from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

from encbackup import vault
from encbackup.config import Config, load


@dataclass
class World:
    root: Path
    data: Path
    recovery: Path  # offline identity file
    recovery_key: str
    write_config: Callable[..., Path]

    @property
    def cfg(self) -> Config:
        return load(self.root / "encbackup.toml")

    def init(self) -> Config:
        cfg = self.cfg
        vault.init(cfg, [self.recovery_key])
        return cfg


def make_identity(path: Path) -> str:
    if shutil.which("age-keygen") is None:
        return ""  # no age here: tests that need identities are skipped
    subprocess.run(["age-keygen", "-o", str(path)], check=True, capture_output=True)
    path.chmod(0o600)
    out = subprocess.run(
        ["age-keygen", "-y", str(path)], check=True, capture_output=True, text=True
    )
    return out.stdout.strip()


tools = pytest.mark.tools
