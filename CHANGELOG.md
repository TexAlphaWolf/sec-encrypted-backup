# Changelog

## 1.0.0

First release. Wraps restic and age: the repository password is generated, kept only age-encrypted to this machine's key and an offline recovery key, and handed to restic through its environment. `backup` applies a retention policy, `verify` reads back part of the stored data and restores a sample of files that it compares byte for byte with the live ones (files edited since the snapshot are counted, not blamed), `rotate` changes the restic password (and optionally this machine's age identity) in an order that survives a crash, `restore` works with the recovery identity alone, `status` reads a journal and prints text, JSON or Prometheus metrics. A Docker image, systemd units, alert rules, and a tool that measures detection of damaged repositories, rotation and secrets at rest.
