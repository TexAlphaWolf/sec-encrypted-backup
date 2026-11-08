"""Command line. Exit codes: 0 fine, 1 a check failed or the backup is stale, 2 error, 3 incomplete backup."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from encbackup import __version__, age, backup, restic, rotate, status, vault, verify
from encbackup.config import Config, load
from encbackup.proc import ToolError, redact

OK, FAILED, ERROR, INCOMPLETE = 0, 1, 2, 3


def human(n: int) -> str:
    size = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{n} B"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="encbackup",
        description="Encrypted backups (restic + age) that are verified by restoring them.",
    )
    p.add_argument(
        "-c",
        "--config",
        type=Path,
        default=Path(os.environ.get("ENCBACKUP_CONFIG", "encbackup.toml")),
    )
    p.add_argument("--version", action="version", version=f"encbackup {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser(
        "init", help="create the identity, the repository password and the repository"
    )
    s.add_argument(
        "--recovery-recipient",
        action="append",
        default=[],
        metavar="AGE1...",
        help="public key of an identity kept offline (repeatable, required)",
    )

    s = sub.add_parser(
        "backup", help="back up the configured paths, then apply the retention policy"
    )
    s.add_argument("--verify", action="store_true", help="verify by restore right after")
    s.add_argument("--json", action="store_true")

    s = sub.add_parser(
        "verify", help="check the repository and restore a sample against the live files"
    )
    s.add_argument("--snapshot", default="latest")
    s.add_argument(
        "--recovery-identity", type=Path, help="an offline identity: prove it opens the password"
    )
    s.add_argument(
        "--data-subset", help="how much stored data to read back, e.g. 5%% or 1/10 (config default)"
    )
    s.add_argument("--sample", type=int, help="number of files to restore (0 = all)")
    s.add_argument("--seed", help="sample selection seed (default: the snapshot id)")
    s.add_argument("--full", action="store_true", help="read back all stored data")
    s.add_argument("--json", action="store_true")

    s = sub.add_parser("restore", help="restore a snapshot into an empty directory")
    s.add_argument("--snapshot", default="latest")
    s.add_argument("--target", type=Path, required=True)
    s.add_argument("--include", action="append", default=[], metavar="PATH")
    s.add_argument(
        "--identity",
        type=Path,
        help="unlock with this identity (e.g. the recovery one) instead of the host's",
    )

    s = sub.add_parser(
        "rotate", help="new repository password; optionally a new identity for this machine"
    )
    s.add_argument(
        "--new-identity", action="store_true", help="also replace this machine's age identity"
    )
    s.add_argument("--keep-other-keys", action="store_true", help="do not remove other restic keys")

    s = sub.add_parser("status", help="state from the journal; exit 1 if stale or failing")
    s.add_argument("--format", choices=("text", "json", "prometheus"), default="text")
    return p


def cmd_init(cfg: Config, a: argparse.Namespace) -> int:
    info = vault.init(cfg, a.recovery_recipient)
    print(f"repository  {redact(cfg.repository)}")
    print(
        f"identity    {cfg.identity} ({'created' if info['identity_created'] else 'existing'}; keep it private)"
    )
    print(f"recipients  {info['recipients']} ({info['host_recipient']} + recovery)")
    print(f"password    {cfg.password_file} (age-encrypted; never written in clear)")
    return OK


def cmd_backup(cfg: Config, a: argparse.Namespace) -> int:
    r = backup.run(cfg)
    if a.json:
        print(json.dumps(r, indent=2))
    else:
        print(
            f"snapshot {r['snapshot']}: {r['files']} files, {human(r['bytes'])}; {r['files_new']} new, {r['files_changed']} changed; stored {human(r['added_bytes'])} in {r['seconds']} s"
        )
        if r["incomplete"]:
            print("warning: some files could not be read; the snapshot is incomplete")
        if "retention_error" in r:
            print(f"warning: retention failed: {r['retention_error']}")
    code = INCOMPLETE if r["incomplete"] else OK
    if a.verify:
        code = max(
            code,
            cmd_verify(
                cfg,
                argparse.Namespace(
                    snapshot=r["snapshot_id"],
                    recovery_identity=None,
                    data_subset=None,
                    sample=None,
                    seed=None,
                    full=False,
                    json=a.json,
                ),
            ),
        )
    return code


def cmd_verify(cfg: Config, a: argparse.Namespace) -> int:
    rep = verify.run(
        cfg,
        snapshot=a.snapshot,
        recovery_identity=a.recovery_identity,
        subset=a.data_subset,
        sample=a.sample,
        seed=a.seed,
        full=a.full,
    )
    vault.journal(
        cfg, "verify", rep.ok, snapshot=rep.snapshot, compared=rep.compared, sampled=rep.sampled
    )
    if a.json:
        print(json.dumps(rep.as_dict(), indent=2))
    else:
        print(f"verify snapshot {rep.snapshot or '-'}")
        for c in rep.checks:
            print(f"  [{'ok' if c.ok else 'FAIL'}] {c.name}: {c.detail}")
        print(
            f"  {rep.compared} of {rep.sampled} sampled files compared byte for byte ({human(rep.bytes_compared)}); {rep.changed_since} changed and {rep.gone_since} gone since the snapshot; {rep.seconds} s"
        )
        print("result: " + ("OK" if rep.ok else "FAILED"))
    return OK if rep.ok else FAILED


def cmd_restore(cfg: Config, a: argparse.Namespace) -> int:
    target: Path = a.target
    if target.exists() and (not target.is_dir() or any(target.iterdir())):
        raise ToolError(f"{target} exists and is not an empty directory; restore into a new one")
    r = vault.restic_for(cfg, a.identity)
    snap = (
        r.latest()
        if a.snapshot == "latest"
        else next((s for s in r.snapshots() if str(s["id"]).startswith(a.snapshot)), None)
    )
    if snap is None:
        raise ToolError(f"no snapshot matches {a.snapshot!r}")
    target.mkdir(parents=True, exist_ok=True)
    r.restore(str(snap["id"]), target, a.include or None)
    print(f"restored snapshot {str(snap['id'])[:8]} into {target} (content verified by restic)")
    return OK


def cmd_rotate(cfg: Config, a: argparse.Namespace) -> int:
    r = rotate.run(cfg, new_identity=a.new_identity, keep_other_keys=a.keep_other_keys)
    print(
        f"restic key   {r['old_key_id']} -> {r['new_key_id']}; removed {len(r['removed_keys'])} other key(s)"
    )
    if r["identity_rotated"]:
        print(
            f"identity     new host key {r['host_recipient']}; the old file is kept as {r['retired_identity']} (delete it when you are sure)"
        )
    print("check        the old password no longer opens the repository; the repository checks out")
    print(f"note         {r['note']}")
    return OK


def cmd_status(cfg: Config, a: argparse.Namespace) -> int:
    info = status.collect(cfg)
    if a.format == "json":
        print(json.dumps(info, indent=2, default=str))
    elif a.format == "prometheus":
        print(status.prometheus(info), end="")
    else:
        for event in ("backup", "verify", "rotate"):
            e = info[event]
            print(
                f"{event:7} "
                + (f"{e['time']}  {'ok' if e.get('ok') else 'FAILED'}" if e else "never")
            )
        for w in info["warnings"]:
            print(f"warning: {w}")
        for pr in info["problems"]:
            print(f"problem: {pr}")
        print("result: " + ("OK" if info["ok"] else "NOT OK"))
    return OK if info["ok"] else FAILED


COMMANDS = {
    "init": cmd_init,
    "backup": cmd_backup,
    "verify": cmd_verify,
    "restore": cmd_restore,
    "rotate": cmd_rotate,
    "status": cmd_status,
}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        cfg = load(args.config)
        if args.command != "status":
            restic.require_version()
            age.version()
        return COMMANDS[args.command](cfg, args)
    except ToolError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return ERROR
