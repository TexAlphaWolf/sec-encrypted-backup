"""Key rotation: a new repository password and, optionally, a new identity for this machine.

What changes: the restic key that wraps the repository's master key, the age-encrypted copy of
the password, and (with --new-identity) which age key this machine uses. What does not: the
master key and the stored data (restic wraps one master key per repository; rotating the
password does not re-encrypt data). docs/limitations.md says what that means.

Order of steps, so that a crash at any point leaves a repository that can still be opened:
  1. add the new restic key (authenticated with the old one);
  2. store the new password encrypted to every recipient (old and new host key during an
     identity change), checking that the identity in use can read it back;
  3. swap the identity file, then drop the old host recipient;
  4. remove every other restic key and check that the old password no longer works.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from encbackup import age, vault
from encbackup.config import Config
from encbackup.proc import ToolError
from encbackup.restic import Restic


def run(
    cfg: Config, *, new_identity: bool = False, keep_other_keys: bool = False
) -> dict[str, Any]:
    old_password = vault.unlock(cfg)
    old = Restic(cfg.repository, old_password, cfg.cache_dir, cfg.timeout)
    current = [k for k in old.key_list() if k.get("current")]
    if len(current) != 1:
        raise ToolError("cannot tell which restic key is in use")
    old_key_id = str(current[0]["id"])
    recipients = vault.read_recipients(cfg)

    new_password = vault.new_password()
    with vault.secret_file(new_password) as password_file:
        old.add_key(password_file)
    new = Restic(cfg.repository, new_password, cfg.cache_dir, cfg.timeout)
    if not new.can_unlock():
        raise ToolError(
            "the new key was added but does not open the repository; the old password still works"
        )

    retired: Path | None = None
    result: dict[str, Any] = {"identity_rotated": new_identity}
    if new_identity:
        candidate = cfg.identity.with_name(cfg.identity.name + ".new")
        if candidate.exists():
            raise ToolError(
                f"{candidate} exists from an interrupted rotation; inspect it, then remove it"
            )
        new_host = age.generate_identity(candidate)
        old_host = next(key for role, key in recipients if role == "host")
        transition = [*recipients, ("host", new_host)]
        _store(cfg, new_password, transition, candidate)  # either identity opens it
        retired = cfg.identity.with_name(f"{cfg.identity.name}.retired")
        n = 1
        while retired.exists():
            n += 1
            retired = cfg.identity.with_name(f"{cfg.identity.name}.retired{n}")
        os.replace(cfg.identity, retired)
        os.replace(candidate, cfg.identity)
        final = [(role, key) for role, key in transition if key != old_host]
        _store(cfg, new_password, final, cfg.identity)
        result["host_recipient"] = new_host
        result["retired_identity"] = str(retired)
    else:
        _store(cfg, new_password, recipients, cfg.identity)

    removed: list[str] = []
    if not keep_other_keys:
        current_new = [k for k in new.key_list() if k.get("current")]
        if len(current_new) != 1:
            raise ToolError("cannot tell which restic key is in use after the change")
        for key in new.key_list():
            if key["id"] != current_new[0]["id"]:
                new.remove_key(str(key["id"]))
                removed.append(str(key["id"]))
        if old.can_unlock():
            raise ToolError("the old password still opens the repository after its key was removed")
    new.check(None)
    result.update(
        old_key_id=old_key_id,
        new_key_id=str(next(k["id"] for k in new.key_list() if k.get("current"))),
        removed_keys=removed,
        note="the repository's master key and stored data are unchanged (see docs/limitations.md)",
    )
    vault.journal(cfg, "rotate", True, identity_rotated=new_identity, removed_keys=len(removed))
    return result


def _store(cfg: Config, password: str, recipients: list[tuple[str, str]], identity: Path) -> None:
    """Write the encrypted password and the recipient list; refuse unless `identity` reads it back."""
    blob = age.encrypt(password.encode("ascii"), [key for _, key in recipients])
    if age.decrypt(blob, identity).decode("ascii") != password:
        raise ToolError("the encrypted password does not decrypt to itself; nothing was written")
    vault.atomic_write(cfg.password_file, blob)
    vault.write_recipients(cfg, recipients)
