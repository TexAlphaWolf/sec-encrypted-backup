# Security policy

encbackup handles the key to every backup, so a way to learn the repository password, to make it write it somewhere readable, to make `verify` report a damaged backup as healthy, or to make rotation leave an old key working would be a vulnerability. Please use GitHub's **private vulnerability reporting** (Security tab, "Report a vulnerability") rather than a public issue. You will get an answer within a few days.

Problems in restic or age themselves go to their projects. [docs/limitations.md](docs/limitations.md) lists what encbackup does not protect against (a compromised machine that holds the host identity, for instance); that is a design limit, not a vulnerability.

Supported: the latest release.

What the code does: the password is generated from `secrets`, stored only age-encrypted (armoured, mode 0600), and handed to restic through the child's environment, never on a command line; no command is run through a shell; ambient `RESTIC_*` settings are removed so they cannot redirect a run; identity files that other users can read are refused; error messages are scrubbed of URL credentials and age secret keys; files are replaced atomically; the temporary file used to add a new restic key is 0600 in a 0700 directory and removed at once.
