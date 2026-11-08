"""Flip every byte of a restic key file, one at a time, and see whether the repository still opens.

python tools/key_flips.py            # about a minute: restic derives the key with scrypt on every try

restic authenticates the encrypted part of a key file; the plain fields in front of it
(created, username, hostname) are not authenticated, so a change there is harmless and unnoticed.
This tool shows that those fields are the *only* place where damage goes unnoticed.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

PASSWORD = "k" * 20


def opens(repo: Path) -> bool:
    env = {
        **os.environ,
        "RESTIC_PASSWORD": PASSWORD,
        "RESTIC_CACHE_DIR": str(repo.parent / "cache"),
    }
    for name in ("RESTIC_PASSWORD_FILE", "RESTIC_PASSWORD_COMMAND", "RESTIC_REPOSITORY"):
        env.pop(name, None)
    r = subprocess.run(
        ["restic", "-r", str(repo), "cat", "config"],
        env=env,
        capture_output=True,
        check=False,
        timeout=120,
    )
    return r.returncode == 0


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp) / "base"
        env = {**os.environ, "RESTIC_PASSWORD": PASSWORD}
        subprocess.run(
            ["restic", "init", "-r", str(base), "--quiet"], env=env, check=True, capture_output=True
        )
        key_file = next((base / "keys").iterdir())
        raw = key_file.read_bytes()
        metadata_end = raw.index(b'"kdf"')

        def trial(offset: int) -> tuple[int, bool]:
            work = Path(tmp) / f"t{offset}"
            shutil.copytree(base, work)
            damaged = bytearray(raw)
            damaged[offset] ^= 0x01
            (work / "keys" / key_file.name).write_bytes(bytes(damaged))
            try:
                return offset, opens(work)
            finally:
                shutil.rmtree(work)

        with ThreadPoolExecutor(max_workers=os.cpu_count() or 4) as pool:
            results = list(pool.map(trial, range(len(raw))))
    unnoticed = [o for o, ok in results if ok]
    inside = [o for o in unnoticed if o < metadata_end]
    summary = {
        "bytes": len(raw),
        "metadata_bytes": metadata_end,
        "unnoticed": len(unnoticed),
        "unnoticed_inside_metadata": len(inside),
        "noticed_outside_metadata": sum(1 for o, ok in results if o >= metadata_end and not ok),
        "bytes_outside_metadata": len(raw) - metadata_end,
    }
    print(json.dumps(summary))
    return (
        0
        if len(inside) == len(unnoticed)
        and summary["noticed_outside_metadata"] == summary["bytes_outside_metadata"]
        else 1
    )


if __name__ == "__main__":
    sys.exit(main())
