# Contributing

```bash
sudo apt-get install restic age                  # or: brew install restic age
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
ruff check . && ruff format --check . && mypy && pytest -n auto --cov
python tools/evaluate.py
```

- **Standard library only for the package.** restic and age are programs it calls, found in `PATH` (or `ENCBACKUP_RESTIC` / `ENCBACKUP_AGE`).
- **Tests use the real restic and age**, not mocks: the interesting failures are in how they actually behave. `ENCBACKUP_REQUIRE_TOOLS=1` turns a missing program into a failure instead of a skip (CI sets it).
- **No shell, no secret on a command line.** Anything that runs a program goes through `proc.run`; anything it prints goes through `proc.redact`.
- **A new failure mode** gets a test that damages a real repository and a line in `tools/evaluate.py` if it is a claim the README makes.
- Tests generate their own keys; never commit identities, repositories or journals.
