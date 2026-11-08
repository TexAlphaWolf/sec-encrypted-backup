# Limitations

What encbackup does not do, and what it does not protect against. Read this before trusting it with data you cannot lose.

## Security model

- **A compromised machine loses the backups.** The host identity opens the repository password, and the same machine can write to the repository: whoever controls it can read every snapshot and delete them. Encryption protects the copy at rest and in transit, not the backup from the machine that makes it. Against ransomware, give the host a repository it can only append to (restic's `rest-server --append-only`, an S3 bucket with object lock) and keep the recovery identity elsewhere.
- **Rotation does not re-encrypt the data.** restic wraps one random master key per repository with each password. `rotate` adds a new password-wrapped copy of that key and removes the others, so the old password stops working on the repository as it is now. But the master key and the stored data are unchanged: someone who copied the repository's `keys/` files *and* knew the old password before the rotation can still read what was copied. Only a new repository (`restic init --copy-chunker-params`, then `restic copy` from the old one) gets a new master key; encbackup does not automate that.
- **The password is in the environment of restic while it runs.** It is never on a command line and never on disk in clear, but root, and the same user, can read a process environment (`/proc/<pid>/environ`). restic's `--password-command` would avoid that; it is not used because the quoting of a command string differs between shells and platforms.
- **The journal is not tamper-evident.** `status` trusts `journal.jsonl`; an attacker who can edit it can make a dead backup look alive. Alert on `encbackup_*_timestamp_seconds` from a machine other than the one being backed up.
- **age plugins (YubiKey, TPM) are not supported**: recipients are native X25519 `age1...` keys. A hardware-backed recovery key is a natural next step.
- **The recovery key is the whole disaster plan.** If the host identity and the recovery identity are both lost, the data is gone; nothing can be done. `verify --recovery-identity` proves the recovery key still opens the password, not that you can find it in five years.

## What `verify` shows, and what it does not

- It reads back a **subset** of the stored data (5% by default) and restores a **sample** of files (200 by default). A damaged byte in data neither step touches is found only by `verify --full`; [docs/evaluation.md](evaluation.md) measures how often the default settings catch it.
- A file is compared with the live source only if its size and modification time are unchanged since the snapshot. A busy file system will have many files that are only counted as "changed since the snapshot"; if *no* sampled file can be compared, the check fails rather than pass on nothing.
- Content is compared; permissions, ownership, extended attributes and timestamps of the restored files are not.
- Files whose path contains `* ? [ ] \` are not sampled (restic's include patterns would treat them as patterns); they are counted in the report.
- The restore goes to a scratch directory in the system temporary directory and needs room for the sampled files.

## Platforms and versions

- **Linux and macOS, and the Docker image, are the supported platforms.** On Windows the code paths that do not call restic or age are tested in CI (configuration, status, path mapping, redaction); a Windows backup is not, because the CI runner has neither program. Use the Docker image there.
- Tested with restic 0.16.4 and age 1.1.1 on the development machine, and with the versions Debian ships in the Docker image and Ubuntu ships on the CI runners (the CI log prints them). restic older than 0.16 is refused.
- One `encbackup` at a time per vault: restic locks the repository, but two `rotate` runs racing on the vault files are not guarded.
- Retention is applied after each backup with `forget --prune`, which locks the repository for the duration of the prune; a very large repository may prefer to prune on a separate schedule (leave `[retention]` out and run restic yourself).
