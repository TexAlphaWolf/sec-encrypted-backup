# Evaluation

Everything here is measured by [`tools/evaluate.py`](../tools/evaluate.py) (and `tools/key_flips.py`) against the real restic 0.16.4 and age 1.1.1, on Linux, Python 3.13. The raw numbers are in [`report.json`](report.json) and [`key_flips.json`](key_flips.json); `tests/test_docs.py` fails if this page and the JSON disagree. CI re-runs a smaller version on every push and fails on a wrong result.

Data set: 60 files, 14.6 MiB (half text-like, half random bytes, sizes from 1 KB to 1.3 MB), generated from a fixed seed. Times are for one run on a small virtual machine: read them as orders of magnitude.

## 1. Size and speed

| | |
|---|---|
| First backup | 0.83 s, 14.6 MiB read |
| Repository after it (compressed, deduplicated, encrypted) | 10.9 MiB |
| Backup again, nothing changed | 0.71 s, **362 bytes** stored |
| After editing 3 of the 60 files | 314,144 bytes stored (restic re-stores only the changed chunks) |
| What encbackup adds to every command: decrypting the password with age | **2.8 ms** |
| `verify` of all 60 files (restore + SHA-256 against the live files) | 2.65 s, 60 of 60 compared |

The wrapper's cost is negligible next to restic's own key derivation (about a second per command); a backup run is restic's speed.

## 2. Damaged repositories

One random byte of one file of a repository is changed (XOR with a random non-zero value), then `verify` runs. 40 trials per row, 95% Wilson intervals.

**Damage inside the stored data** (a data pack, chosen with probability proportional to its size):

| `verify` setting | damage found |
|---|---|
| read 5% of the data, restore **1** file (a small `--sample`) | 16 of 40 (40.0%; 95% CI 26.3% to 55.4%) |
| read 5% of the data, restore **all** files | 39 of 40 (97.5%; 95% CI 87.1% to 99.6%) |
| read **100%** of the data (`--full`) | **40 of 40** (100%; 95% CI 91.2% to 100%) |

Two things this shows:

- **The defaults trade thoroughness for time, and the trade is real.** Reading 5% of the data and restoring a small sample finds a quarter to a half of random single-byte damage; restoring everything finds nearly all of it; only reading everything is certain. Pick the setting from how long a full read takes against how often you run it (a weekly `verify --full` and a nightly default is a reasonable split).
- **A restore tests the newest snapshot only.** The one miss in the "restore all files" row (and 5 of the 24 misses in the first row) was damage in data that only an older snapshot uses: restoring the first snapshot of the same damaged repository revealed it. A full read has no such blind spot.

**Damage elsewhere** (default settings):

| File damaged | found |
|---|---|
| index file | 20 of 20 |
| snapshot file | 20 of 20 |
| key file, outside its plain-text header | 28 of 28 |

The key file begins with a plain-text header (`created`, `username`, `hostname`, 83 bytes) that restic does not authenticate: of 12 trials that landed there, 9 changed something restic noticed and 3 did not. That is harmless (it is a label), and `tools/key_flips.py` shows it is the only such place: flipping each of the 444 bytes of a key file in turn, the repository still opened for 56 of them, **all 56 inside the first 83 bytes**; all 361 other bytes were noticed.

## 3. Key rotation

6 rotations in a row on the same repository, every second one with `--new-identity`:

| | |
|---|---|
| Median time | 10.7 s (about ten restic commands, each deriving the key) |
| Old password rejected afterwards | 6 of 6 |
| Exactly one restic key left | 6 of 6 |
| `verify` passes after each | 6 of 6 |
| Old passwords (of all rotations) that still open the repository at the end | **0** |
| The offline recovery key opens the current password after each | yes |

Rotation changes the password that wraps the master key, not the master key or the data; see [limitations](limitations.md).

## 4. Secrets at rest

After a session with a backup, 6 rotations (3 of them with a new identity), every file outside the repository itself (the vault, the journal, the cache, the configuration, the identity files) was searched for the 7 passwords the repository has had: **0 found**. Identity files, the encrypted password and the vault have mode 0600 (no private file readable by others).

## 5. Scenarios

`scenarios correct: 8 of 8`: a stranger's identity cannot open the password; an identity file readable by others is refused; `init` refuses to run twice; a missing repository is named as missing; credentials in a repository URL are not echoed in an error; ambient `RESTIC_REPOSITORY` / `RESTIC_PASSWORD` do not redirect a run; a file edited after the snapshot is counted as changed rather than as a failure; a three-day-old backup is a problem in `status`.

## 6. What the tests add

More than 100 tests (about 95% line coverage, branch coverage on) run the real programs: a backup restored byte for byte, damaged packs and truncated files, a crash after the new key was added, an interrupted identity rotation, a copy of the vault on a machine with no host key restoring with the recovery key alone, secrets absent from every command's output. The one test that needs a non-root user (an unreadable file gives an incomplete snapshot) is skipped when run as root and passes as an ordinary user.

## 7. What none of this shows

- Behaviour with repositories of hundreds of gigabytes: the data set here is 14.6 MiB. Timings do not extrapolate; the `verify` settings matter more as the repository grows.
- Remote back ends (S3, SFTP): only local repositories were measured; the code passes them to restic unchanged.
- That restic and age are correct. The damaged-repository results are a property of restic's checks, which encbackup uses and reports.
- Windows backups (see [limitations](limitations.md)).
