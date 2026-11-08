# Architecture

```mermaid
flowchart LR
    subgraph host[This machine]
      cfg[encbackup.toml]
      id[host.key<br/>age identity, 0600]
      vault[vault/<br/>repo.key.age · recipients.txt · journal.jsonl]
      eb[encbackup]
      src[(data to protect)]
    end
    rec[recovery.key<br/>offline age identity]
    age[[age]]
    restic[[restic]]
    repo[(restic repository<br/>local, sftp, s3, rest, rclone)]

    cfg --> eb
    eb -- 1. decrypt password --> age
    id --> age
    vault --> age
    age -- password, in memory --> eb
    eb -- 2. RESTIC_PASSWORD in the child's environment --> restic
    src --> restic
    restic -- encrypted, deduplicated --> repo
    rec -. can open the same password .-> vault
```

**One secret, two ways in.** `init` generates a 384-bit password, creates the repository with it, and stores it only as an armoured age file encrypted to two recipients: this machine's identity and an offline recovery key. Every command decrypts it with age and gives it to restic through the child's environment.

**Rotation order** (`rotate`), chosen so that a crash at any step leaves a repository that can still be opened:

```mermaid
sequenceDiagram
    participant E as encbackup
    participant R as restic
    participant V as vault
    E->>R: add new key (authenticated with the old one)
    E->>V: write new password, encrypted to old+new host key and recovery key; read it back
    E->>V: swap identity file (new identity in place, old one kept as .retired)
    E->>V: rewrite for new host key and recovery key only
    E->>R: remove every other key; prove the old password is refused
    E->>R: check
```

**Verification** (`verify`): recovery key opens the same password → `restic check --read-data-subset` → restore a sample into a scratch directory (`restic restore --verify` checks every file against restic's own hashes) → compare each unchanged file with its live source by SHA-256.
