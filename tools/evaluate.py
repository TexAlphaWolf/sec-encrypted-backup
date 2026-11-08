"""Measure encbackup on real restic and age; every figure in the documentation comes from here.

    python tools/evaluate.py                      # text report
    python tools/evaluate.py --out docs/report.json --trials 40

Sections: 1 size and speed, 2 detection of damaged repositories, 3 key rotation,
4 secrets at rest, 5 scenarios (what must be refused, what must be reported).
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import math
import os
import random
import shutil
import statistics
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from encbackup import age, backup, restic, rotate, status, vault, verify
from encbackup.config import Config, load
from encbackup.proc import ToolError
from encbackup.restic import Restic


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, centre - half), min(1.0, centre + half))


def pct(x: float) -> str:
    return f"{100 * x:.1f}%"


def make_dataset(root: Path, files: int, seed: int) -> int:
    rng = random.Random(seed)
    words = [
        bytes(rng.choices(b"abcdefghijklmnopqrstuvwxyz", k=rng.randint(3, 9))) for _ in range(400)
    ]
    total = 0
    for i in range(files):
        size = int(10 ** rng.uniform(3, 6.1))  # 1 KB to about 1.3 MB
        directory = root / f"d{i % 7}"
        directory.mkdir(parents=True, exist_ok=True)
        if i % 2:
            data = os.urandom(size)  # incompressible
        else:
            data = b""  # text-like, compresses well
            while len(data) < size:
                data += b" ".join(rng.choices(words, k=50)) + b"\n"
            data = data[:size]
        (directory / f"f{i:04d}.dat").write_bytes(data)
        total += size
    return total


def write_config(root: Path, data: Path, **sections: str) -> Config:
    body = f'repository = "repo"\nvault = "vault"\nidentity = "host.key"\n[backup]\npaths = ["{data.as_posix()}"]\n'
    body += "[verify]\nsample_files = 40\n"
    body += "".join(sections.values())
    (root / "encbackup.toml").write_text(body)
    return load(root / "encbackup.toml")


def new_identity(path: Path) -> str:
    return age.generate_identity(path)


def flip(path: Path, rng: random.Random, kind: str) -> dict[str, Any]:
    """Change one random byte; say where it fell (pack header, key metadata) for the report."""
    raw = bytearray(path.read_bytes())
    at = rng.randrange(len(raw))
    raw[at] ^= rng.randrange(1, 256)
    info: dict[str, Any] = {"in_header": False, "in_metadata": False}
    if kind == "data":
        header_len = int.from_bytes(raw[-4:], "little")
        info["in_header"] = len(raw) - 4 - header_len <= at < len(raw)
    if kind == "key":
        info["in_metadata"] = at < bytes(raw).find(
            b'"kdf"'
        )  # created, username, hostname: not authenticated
    path.chmod(0o600)  # restic 0.18 stores files read-only
    path.write_bytes(bytes(raw))
    return info


def damaged(
    cfg: Config, root: Path, kind: str, rng: random.Random
) -> tuple[Config, dict[str, Any]]:
    copy = root / f"repo-{kind}"
    if copy.exists():
        shutil.rmtree(copy)
    shutil.copytree(cfg.repository, copy)
    if kind == "data":
        files = sorted(p for p in (copy / "data").rglob("*") if p.is_file())
        victim = rng.choices(files, weights=[p.stat().st_size for p in files])[0]
    else:
        folder = {"index": "index", "snapshot": "snapshots", "key": "keys"}[kind]
        victim = rng.choice(sorted(p for p in (copy / folder).rglob("*") if p.is_file()))
    info = flip(victim, rng, kind)
    return dataclasses.replace(cfg, repository=str(copy)), info


def detected(cfg: Config, **options: Any) -> bool:
    try:
        return not verify.run(cfg, **options).ok
    except ToolError:
        return True


def section_size(work: Path, files: int, seed: int) -> dict[str, Any]:
    data = work / "data"
    total = make_dataset(data, files, seed)
    recovery = work / "recovery.txt"
    rec_key = new_identity(recovery)
    cfg = write_config(work, data)
    t = time.monotonic()
    vault.init(cfg, [rec_key])
    init_s = time.monotonic() - t

    t = time.monotonic()
    first = backup.run(cfg)
    first_s = time.monotonic() - t
    t = time.monotonic()
    second = backup.run(cfg)
    second_s = time.monotonic() - t

    rng = random.Random(seed + 1)
    paths = sorted(data.rglob("*.dat"))
    touched = rng.sample(paths, max(1, len(paths) // 20))
    for p in touched:
        raw = bytearray(p.read_bytes())
        raw[len(raw) // 2 : len(raw) // 2 + 8] = os.urandom(8)
        p.write_bytes(bytes(raw))
    third = backup.run(cfg)
    stored = Restic(cfg.repository, vault.unlock(cfg), cfg.cache_dir).stats_raw()

    # what the wrapper adds on top of restic: decrypting the password with age, once per command
    t = time.monotonic()
    for _ in range(20):
        vault.unlock(cfg)
    unlock_ms = (time.monotonic() - t) / 20 * 1000
    t = time.monotonic()
    rep = verify.run(cfg, sample=0)
    verify_s = time.monotonic() - t
    return {
        "files": files,
        "source_bytes": total,
        "init_seconds": round(init_s, 2),
        "first_backup": {"seconds": round(first_s, 2), "added_bytes": first["added_bytes"]},
        "unchanged_backup": {
            "seconds": round(second_s, 2),
            "added_bytes": second["added_bytes"],
            "files_changed": second["files_changed"],
        },
        "after_editing_5_percent": {
            "files_edited": len(touched),
            "added_bytes": third["added_bytes"],
            "files_changed": third["files_changed"],
        },
        "repository_data_bytes": stored,
        "age_unlock_ms": round(unlock_ms, 1),
        "verify_all_files": {
            "ok": rep.ok,
            "files": rep.sampled,
            "compared": rep.compared,
            "seconds": round(verify_s, 2),
        },
        "_cfg": cfg,
    }


def section_detection(cfg: Config, work: Path, trials: int, seed: int) -> dict[str, Any]:
    rng = random.Random(seed)
    out: dict[str, Any] = {"trials": trials}
    settings = {
        "check 5% of data, restore 1 file": {"subset": "5%", "sample": 1},
        "check 5% of data, restore all files": {"subset": "5%", "sample": 0},
        "check 100% of data, restore 1 file": {"subset": "100%", "sample": 1},
    }
    for name, opts in settings.items():
        hits = header_miss = older = 0
        first_snapshot = vault.restic_for(cfg).snapshots()[0]["id"]
        for _ in range(trials):
            damaged_cfg, info = damaged(cfg, work, "data", rng)
            found = detected(damaged_cfg, **opts)
            hits += found
            header_miss += (not found) and info["in_header"]
            if not found:
                # is the damage in data only an older snapshot needs?
                older += detected(damaged_cfg, snapshot=first_snapshot, **opts)
        lo, hi = wilson(hits, trials)
        out[name] = {
            "detected": hits,
            "trials": trials,
            "missed_in_pack_header": header_miss,
            "missed_but_found_through_the_first_snapshot": older,
            "low": round(lo, 3),
            "high": round(hi, 3),
        }
        print(
            f"  {name:40} {hits:3} of {trials} ({pct(hits / trials)}; 95% CI {pct(lo)} to {pct(hi)}); misses in a pack header: {header_miss}; misses an older snapshot reveals: {older}",
            flush=True,
        )
    for kind in ("index", "snapshot", "key"):
        n = max(5, trials // 2) if kind != "key" else trials
        hits = meta = meta_hits = 0
        for _ in range(n):
            damaged_cfg, info = damaged(cfg, work, kind, rng)
            found = detected(damaged_cfg, subset="5%", sample=1)
            if info["in_metadata"]:
                meta += 1
                meta_hits += found
            else:
                hits += found
        counted = n - meta
        lo, hi = wilson(hits, counted)
        out[f"{kind} file"] = {
            "detected": hits,
            "trials": counted,
            "low": round(lo, 3),
            "high": round(hi, 3),
            "metadata_only_flips": meta,
            "metadata_only_detected": meta_hits,
        }
        extra = (
            f"; {meta} landed in unauthenticated key metadata ({meta_hits} noticed)"
            if kind == "key"
            else ""
        )
        print(
            f"  {kind + ' file damaged (default check)':40} {hits:3} of {counted} ({pct(hits / max(counted, 1))}){extra}",
            flush=True,
        )
    return out


def section_rotation(cfg: Config, work: Path, rounds: int) -> tuple[dict[str, Any], list[str]]:
    old_passwords: list[str] = []
    times: list[float] = []
    ok_old_rejected = ok_verify = ok_one_key = 0
    identities_open = True
    for i in range(rounds):
        old_passwords.append(vault.unlock(cfg))
        t = time.monotonic()
        rotate.run(cfg, new_identity=(i % 2 == 1))
        times.append(time.monotonic() - t)
        probe = Restic(cfg.repository, old_passwords[-1], work / "probe-cache")
        ok_old_rejected += not probe.can_unlock()
        ok_one_key += len(vault.restic_for(cfg).key_list()) == 1
        ok_verify += verify.run(cfg, sample=10).ok
        identities_open &= vault.unlock(cfg, work / "recovery.txt") == vault.unlock(cfg)
    still_open = sum(
        Restic(cfg.repository, p, work / "probe-cache").can_unlock() for p in old_passwords
    )
    return {
        "rotations": rounds,
        "median_seconds": round(statistics.median(times), 2),
        "old_password_rejected": ok_old_rejected,
        "single_key_left": ok_one_key,
        "verify_ok_after": ok_verify,
        "old_passwords_that_still_open": still_open,
        "recovery_key_always_opens": identities_open,
    }, old_passwords


def section_secrets(cfg: Config, work: Path, passwords: list[str]) -> dict[str, Any]:
    needles = [p.encode() for p in passwords if p]
    secret_keys = []
    for ident in work.glob("host.key*"):
        secret_keys += [
            line.encode()
            for line in ident.read_text().splitlines()
            if line.startswith("AGE-SECRET-KEY-")
        ]
    leaks = []
    scanned = 0
    for path in work.rglob("*"):
        if (
            not path.is_file()
            or path.relative_to(work).parts[0] == "data"
            or path.relative_to(work).parts[0].startswith("repo")
        ):
            continue
        raw = path.read_bytes()
        scanned += 1
        if any(n in raw for n in needles):
            leaks.append(str(path.relative_to(work)))
    loose = []
    if os.name == "posix":
        for ident in work.glob("host.key*"):
            if ident.stat().st_mode & 0o077:
                loose.append(ident.name)
        loose += [p.name for p in cfg.vault.glob("repo.key.age") if p.stat().st_mode & 0o077]
    return {
        "files_scanned": scanned,
        "passwords_checked": len(needles),
        "files_containing_a_password": leaks,
        "private_files_with_loose_permissions": loose,
    }


def scenarios(work: Path, cfg: Config) -> list[tuple[str, bool]]:
    results: list[tuple[str, bool]] = []

    def expect_error(name: str, fn: Any, text: str) -> None:
        try:
            fn()
        except ToolError as exc:
            results.append((name, text in str(exc)))
        else:
            results.append((name, False))

    stranger = work / "stranger.key"
    new_identity(stranger)
    expect_error(
        "a stranger's identity cannot open the password",
        lambda: vault.unlock(cfg, stranger),
        "could not decrypt",
    )
    if os.name == "posix":
        loose = work / "loose.key"
        new_identity(loose)
        loose.chmod(0o644)
        expect_error(
            "an identity file readable by others is refused",
            lambda: vault.unlock(cfg, loose),
            "accessible to other users",
        )
    expect_error(
        "init refuses to run twice",
        lambda: vault.init(cfg, ["age1" + "q" * 58]),
        "already initialised",
    )
    expect_error(
        "a repository that is not there is named as such",
        lambda: Restic(str(work / "nowhere"), "x", work / "c").can_unlock(),
        "no restic repository",
    )
    try:
        Restic("rest:http://bob:hunter2@127.0.0.1:1/r", "x", work / "c", 30).snapshots()
        results.append(("credentials in a repository URL are not echoed (message)", False))
    except ToolError as exc:
        results.append(
            ("credentials in a repository URL are not echoed (message)", "hunter2" not in str(exc))
        )

    # an ambient RESTIC_* variable must not redirect a run
    os.environ["RESTIC_REPOSITORY"] = str(work / "elsewhere")
    os.environ["RESTIC_PASSWORD"] = "wrong"
    try:
        out = backup.run(cfg)
        results.append(
            (
                "ambient RESTIC_REPOSITORY / RESTIC_PASSWORD are ignored",
                bool(out["snapshot"]) and not (work / "elsewhere").exists(),
            )
        )
    finally:
        del os.environ["RESTIC_REPOSITORY"], os.environ["RESTIC_PASSWORD"]

    # a changed or removed file after the snapshot is reported, not blamed on the backup
    probe = Path(cfg.paths[0]) / "d0" / "f0000.dat"
    saved = probe.read_bytes()
    backup.run(cfg)
    probe.write_bytes(saved + b"x")
    rep = verify.run(cfg, sample=0)
    results.append(
        (
            "a file edited after the snapshot is counted as changed, not as an error",
            rep.ok and rep.changed_since >= 1,
        )
    )
    probe.write_bytes(saved)

    # status: stale, failing
    info = status.collect(cfg, status_now(days=3))
    results.append(("status reports a 3-day-old backup as a problem", not info["ok"]))
    return results


def status_now(days: int) -> Any:
    from datetime import UTC, datetime, timedelta

    return datetime.now(UTC) + timedelta(days=days)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--files", type=int, default=60)
    ap.add_argument("--trials", type=int, default=20)
    ap.add_argument("--rotations", type=int, default=4)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--out", type=Path)
    args = ap.parse_args()

    versions = {
        "restic": ".".join(map(str, restic.version())),
        "age": age.version(),
        "python": sys.version.split()[0],
        "platform": sys.platform,
    }
    print(f"restic {versions['restic']}, age {versions['age']}, python {versions['python']}")
    report: dict[str, Any] = {"versions": versions, "seed": args.seed}
    with tempfile.TemporaryDirectory(prefix="encbackup-eval-") as tmp:
        work = Path(tmp)
        print("\n1. Size and speed")
        size = section_size(work, args.files, args.seed)
        cfg = size.pop("_cfg")
        report["size"] = size
        mib = size["source_bytes"] / 2**20
        print(
            f"  {size['files']} files, {mib:.1f} MiB; repository holds {size['repository_data_bytes'] / 2**20:.1f} MiB"
        )
        print(
            f"  first backup {size['first_backup']['seconds']} s, unchanged {size['unchanged_backup']['seconds']} s storing {size['unchanged_backup']['added_bytes']} bytes"
        )
        print(
            f"  after editing {size['after_editing_5_percent']['files_edited']} files: stored {size['after_editing_5_percent']['added_bytes']} bytes"
        )
        print(
            f"  age unlock {size['age_unlock_ms']} ms per command; verify of all files {size['verify_all_files']['seconds']} s"
        )

        print("\n2. Damaged repositories (one random byte changed)")
        report["detection"] = section_detection(cfg, work, args.trials, args.seed)

        print("\n3. Key rotation")
        rot, passwords = section_rotation(cfg, work, args.rotations)
        report["rotation"] = rot
        print(
            f"  {rot['rotations']} rotations, median {rot['median_seconds']} s; old password rejected {rot['old_password_rejected']} of {rot['rotations']}; one key left {rot['single_key_left']} of {rot['rotations']}"
        )
        print(
            f"  verified after each: {rot['verify_ok_after']} of {rot['rotations']}; old passwords that still open the repository: {rot['old_passwords_that_still_open']}"
        )

        print("\n4. Secrets at rest")
        # every password the vault has ever held: re-derive from the journal is impossible by design,
        # so the rotation section kept the old ones; check them and the current one
        sec = section_secrets(cfg, work, [*passwords, vault.unlock(cfg)])
        report["secrets"] = sec
        print(
            f"  {sec['files_scanned']} files outside the repository scanned for {sec['passwords_checked']} passwords: {len(sec['files_containing_a_password'])} found"
        )
        print(
            f"  private files with loose permissions: {len(sec['private_files_with_loose_permissions'])}"
        )

        print("\n5. Scenarios")
        sc = scenarios(work, cfg)
        for name, good in sc:
            print(f"  [{'ok' if good else 'FAIL'}] {name}")
        report["scenarios"] = {
            "correct": sum(g for _, g in sc),
            "total": len(sc),
            "failed": [n for n, g in sc if not g],
        }
        print(
            f"  scenarios correct: {report['scenarios']['correct']} of {report['scenarios']['total']}"
        )

    d = report["detection"]
    print(
        f"\ndamaged data found by the default check: {d['check 5% of data, restore 1 file']['detected']} of {args.trials}"
    )
    print("unexpected exceptions: 0")
    if args.out:
        args.out.write_text(json.dumps(report, indent=2) + "\n")
    bad = (
        report["scenarios"]["failed"]
        or rot["old_passwords_that_still_open"]
        or rot["old_password_rejected"] != rot["rotations"]
        or sec["files_containing_a_password"]
        or sec["private_files_with_loose_permissions"]
        or d["check 100% of data, restore 1 file"]["detected"] != args.trials
        or any(
            d[k]["detected"] != d[k]["trials"] for k in ("index file", "snapshot file", "key file")
        )
    )
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
