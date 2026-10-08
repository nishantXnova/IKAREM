"""Supply-chain footprint: what does `pip install <framework>` actually pull in?

Creates one isolated venv per framework, installs it, and reports what
landed: packages pulled beyond a bare venv, installed bytes, and cold
interpreter import time (best of 5 fresh processes).

IKAREM installs from this repo (no deps by construction); rivals come
from PyPI at whatever is latest when you run this — versions are printed
with the numbers so the table stays honest.

Usage: python bench/footprint.py  (needs network; takes a few minutes)
"""

import json
import os
import subprocess
import sys
import sysconfig
import tempfile
import time
import venv
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASELINE = {"pip", "setuptools", "wheel", "pkg_resources"}

TARGETS = {
    "ikarem": {"spec": str(ROOT), "import": "ikarem"},
    "fastapi": {"spec": "fastapi", "import": "fastapi"},
    "flask": {"spec": "flask", "import": "flask"},
    "django": {"spec": "django", "import": "django"},
}


def _venv_python(d: Path) -> str:
    exe = d / ("Scripts" if os.name == "nt" else "bin") / ("python.exe" if os.name == "nt" else "python")
    return str(exe)


def _run(py: str, *args: str) -> str:
    return subprocess.check_output([py, *args], text=True, stderr=subprocess.STDOUT)


def _dir_bytes(p: Path) -> int:
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())


def measure(name: str, spec: str, mod: str, tmp: Path) -> dict:
    d = tmp / f"fp-{name}"
    venv.create(d, with_pip=True)
    py = _venv_python(d)
    purelib = _run(py, "-c", "import sysconfig; print(sysconfig.get_path('purelib'))").strip()
    bare_bytes = _dir_bytes(Path(purelib))
    subprocess.check_call(
        [py, "-m", "pip", "install", "--quiet", "--disable-pip-version-check", spec],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.STDOUT,
    )
    out = _run(py, "-m", "pip", "list", "--format=json")
    # pip may surround the JSON with WARNING lines; decode the first value only
    pkgs = json.JSONDecoder().raw_decode(out[out.index("[") :])[0]
    names = {p["name"] for p in pkgs} - BASELINE
    pulled = sorted(names, key=str.lower)
    version = next((p["version"] for p in pkgs if p["name"].lower() == mod.lower()), "?")
    landed_bytes = _dir_bytes(Path(purelib)) - bare_bytes
    runs = []
    for _ in range(5):
        t0 = time.perf_counter()
        subprocess.check_call([py, "-c", f"import {mod}"], stdout=subprocess.DEVNULL)
        runs.append((time.perf_counter() - t0) * 1000)
    return {
        "name": name,
        "version": version,
        "pulled": pulled,
        "kb": landed_bytes // 1024,
        "import_ms": min(runs),
    }


def main() -> None:
    print(f"python {sys.version.split()[0]} on {sys.platform}; {sysconfig.get_platform()}")
    tmp = Path(tempfile.mkdtemp(prefix="ikarem-footprint-"))
    rows = []
    for name, t in TARGETS.items():
        try:
            rows.append(measure(name, t["spec"], t["import"], tmp))
        except subprocess.CalledProcessError as e:
            print(f"{name}: FAILED ({str(e)[:200]})")
    print("\n| framework | version | packages pulled | installed | cold import |")
    print("|---|---|---|---|---|")
    for r in rows:
        n = len(r["pulled"])
        extra = "" if n == 0 else f" ({', '.join(r['pulled'][:8])}{'…' if n > 8 else ''})"
        print(f"| {r['name']} | {r['version']} | {n}{extra} | {r['kb']:,} kB | {r['import_ms']:.0f} ms |")
    print("\nRerun anytime: `python bench/footprint.py` (isolated venvs, best-of-5 cold imports).")


if __name__ == "__main__":
    main()
