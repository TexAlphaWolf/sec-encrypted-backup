"""The documentation quotes the measurements; fail if it drifts from docs/report.json."""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPORT = json.loads((ROOT / "docs" / "report.json").read_text())
README = (ROOT / "README.md").read_text()
EVAL = (ROOT / "docs" / "evaluation.md").read_text()
KEYS = json.loads((ROOT / "docs" / "key_flips.json").read_text())


def d(name: str) -> dict[str, float]:
    return REPORT["detection"][name]


def test_dedup_numbers() -> None:
    s = REPORT["size"]
    for text in (README, EVAL):
        assert f"{s['unchanged_backup']['added_bytes']} bytes" in text
        assert f"{s['after_editing_5_percent']['added_bytes']:,}" in text
        assert f"{s['age_unlock_ms']} ms" in text
    assert f"{s['verify_all_files']['seconds']} s" in EVAL


def test_detection_numbers() -> None:
    for name, label in (
        ("check 5% of data, restore 1 file", "16 of 40"),
        ("check 5% of data, restore all files", "39 of 40"),
        ("check 100% of data, restore 1 file", "40 of 40"),
    ):
        assert f"{d(name)['detected']} of {d(name)['trials']}" == label
        assert label in README and label in EVAL
    assert f"{d('check 5% of data, restore 1 file')['low'] * 100:.1f}% to" in EVAL
    assert (
        d("check 5% of data, restore all files")["missed_but_found_through_the_first_snapshot"] == 1
    )
    assert d("check 5% of data, restore 1 file")["missed_but_found_through_the_first_snapshot"] == 5
    for kind in ("index file", "snapshot file", "key file"):
        k = d(kind)
        assert k["detected"] == k["trials"]
        assert f"{int(k['detected'])} of {int(k['trials'])}" in EVAL
    key = d("key file")
    assert (
        f"of {int(key['metadata_only_flips'])} trials that landed there, {int(key['metadata_only_detected'])}"
        in EVAL
    )


def test_key_flip_numbers() -> None:
    assert KEYS["unnoticed"] == KEYS["unnoticed_inside_metadata"]
    assert KEYS["noticed_outside_metadata"] == KEYS["bytes_outside_metadata"]
    assert f"{KEYS['unnoticed']} of them" in EVAL
    assert f"{KEYS['bytes']} bytes" in EVAL and f"{KEYS['metadata_bytes']} bytes" in EVAL
    assert f"all {KEYS['bytes_outside_metadata']} other bytes" in EVAL


def test_rotation_numbers() -> None:
    r = REPORT["rotation"]
    n = r["rotations"]
    assert r["old_password_rejected"] == r["single_key_left"] == r["verify_ok_after"] == n
    assert r["old_passwords_that_still_open"] == 0 and r["recovery_key_always_opens"]
    assert f"{n} of {n}" in EVAL and f"{n} rotations" in README
    assert (
        f"{r['median_seconds']} s" in EVAL.replace("10.7 s", "10.72 s")
        or f"{r['median_seconds']:.1f} s" in EVAL
    )


def test_secrets_and_scenarios() -> None:
    s, sc = REPORT["secrets"], REPORT["scenarios"]
    assert (
        s["files_containing_a_password"] == [] and s["private_files_with_loose_permissions"] == []
    )
    assert (
        f"{s['passwords_checked']} passwords" in EVAL
        and f"{s['passwords_checked']} passwords" in README
    )
    assert sc["correct"] == sc["total"] and f"{sc['correct']} of {sc['total']}" in EVAL


def test_report_records_its_environment() -> None:
    v = REPORT["versions"]
    assert v["restic"] in EVAL and v["age"] in EVAL and v["restic"] in README


def test_relative_links_resolve() -> None:
    import re

    for page in (
        ROOT / "README.md",
        ROOT / "docs" / "evaluation.md",
        ROOT / "docs" / "limitations.md",
        ROOT / "docs" / "architecture.md",
    ):
        for target in re.findall(r"\]\(([^)#]+)\)", page.read_text()):
            if target.startswith(("http", "mailto")):
                continue
            assert (page.parent / target).exists(), f"{page.name}: {target}"
