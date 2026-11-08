"""Render docs/demo.png from a real session (real restic, real age, a really damaged repository).

pip install -e ".[dev]" && playwright install chromium
python tools/screenshot.py
"""

from __future__ import annotations

import contextlib
import html
import io
import os
import random
import re
import shutil
from pathlib import Path

from playwright.sync_api import sync_playwright

from encbackup import age, cli

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "demo.png"
WORK = Path("/tmp/encbackup-demo") if os.name == "posix" else Path.home() / "encbackup-demo"


def session() -> list[tuple[str, str]]:
    if WORK.exists():
        shutil.rmtree(WORK)
    data = WORK / "documents"
    rng = random.Random(3)
    for i in range(24):
        d = data / f"project{i % 4}"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"file{i:02d}.dat").write_bytes(rng.randbytes(20_000 + 7_000 * i))
    recovery = WORK / "recovery.key"
    recovery_pub = age.generate_identity(recovery)
    (WORK / "encbackup.toml").write_text(
        f'repository = "{(WORK / "repo").as_posix()}"\nvault = "{(WORK / "vault").as_posix()}"\n'
        f'identity = "{(WORK / "host.key").as_posix()}"\n[backup]\npaths = ["{data.as_posix()}"]\n'
        "[retention]\ndaily = 7\n[verify]\nsample_files = 40\n"
    )
    config = str(WORK / "encbackup.toml")

    steps: list[tuple[str, list[str]]] = [
        (
            "encbackup init --recovery-recipient age1…",
            ["init", "--recovery-recipient", recovery_pub],
        ),
        ("encbackup backup --verify", ["backup", "--verify"]),
        ("encbackup rotate --new-identity", ["rotate", "--new-identity"]),
        (
            "encbackup verify --recovery-identity recovery.key",
            ["verify", "--recovery-identity", str(recovery)],
        ),
    ]
    shots: list[tuple[str, str]] = []
    for label, argv in steps:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            cli.main(["-c", config, *argv])
        shots.append((label, buf.getvalue()))

    # damage one stored pack, as bit rot or a bad disk would
    victim = max(
        (p for p in (WORK / "repo" / "data").rglob("*") if p.is_file()),
        key=lambda p: p.stat().st_size,
    )
    raw = bytearray(victim.read_bytes())
    raw[len(raw) // 3] ^= 0x40
    victim.write_bytes(bytes(raw))
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        cli.main(["-c", config, "verify", "--full"])
    shots.append(
        ("(one byte of a stored pack changes on disk)\nencbackup verify --full", buf.getvalue())
    )
    return shots


def colour(line: str) -> str:
    text = html.escape(line)
    text = re.sub(r"\[ok\]", '[<span class="ok">ok</span>]', text)
    text = re.sub(r"\[FAIL\]", '[<span class="bad">FAIL</span>]', text)
    text = re.sub(r"(result: )(OK)", r'\1<span class="ok">\2</span>', text)
    text = re.sub(r"(result: )(FAILED)", r'\1<span class="bad">\2</span>', text)
    return text


def page(shots: list[tuple[str, str]]) -> str:
    blocks = []
    for label, out in shots:
        cmd = label.split("\n")
        pre = "".join(f'<div class="note">{html.escape(c)}</div>' for c in cmd[:-1])
        pre += f'<div class="cmd"><span class="p">$</span> {html.escape(cmd[-1])}</div>'
        body = "\n".join(colour(ln) for ln in out.rstrip("\n").splitlines())
        blocks.append(f"{pre}<pre>{body}</pre>")
    return f"""<!doctype html><meta charset=utf-8><style>
body{{margin:0;background:#0d1117;font:14px/1.45 ui-monospace,Menlo,Consolas,monospace;color:#c9d1d9}}
.win{{width:1080px;padding:22px 28px 26px}}
.bar{{color:#8b949e;margin-bottom:14px;font-size:13px}}
pre{{margin:2px 0 14px;white-space:pre-wrap;word-break:break-all}}
.cmd{{color:#e6edf3;margin-top:6px}} .p{{color:#58a6ff}} .note{{color:#8b949e;font-style:italic}}
.ok{{color:#3fb950;font-weight:600}} .bad{{color:#f85149;font-weight:600}}
</style><div class=win><div class=bar>encbackup 1.0.0 · restic + age · real run, nothing mocked</div>{"".join(blocks)}</div>"""


def main() -> None:
    shots = session()
    OUT.parent.mkdir(exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        pg = browser.new_page(viewport={"width": 1136, "height": 400}, device_scale_factor=2)
        pg.set_content(page(shots))
        pg.locator(".win").screenshot(path=str(OUT))
        browser.close()
    shutil.rmtree(WORK, ignore_errors=True)
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
