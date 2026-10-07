"""Release hygiene: version sync + Warehouse-valid classifiers.

Regression guard for 1.3.0: `Topic :: Internet :: WWW/HTTP :: ASGI` is not
a real trove classifier, so Warehouse 400'd the upload while `twine check`
stayed green. This test checks against the canonical set instead.
Needs `pip install trove-classifiers` (test-only dep, like pydantic/PyJWT).
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

trove_classifiers = pytest.importorskip(
    "trove_classifiers", reason="pip install trove-classifiers for release-hygiene tests"
)


def _pyproject_classifiers() -> list[str]:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    m = re.search(r"classifiers\s*=\s*\[(.*?)\]", text, re.S)
    assert m, "no classifiers block in pyproject.toml"
    return re.findall(r'"([^"]+)"', m.group(1))


def test_classifiers_are_warehouse_valid():
    bad = [c for c in _pyproject_classifiers() if c not in trove_classifiers.classifiers]
    assert not bad, f"not valid trove classifiers (Warehouse would 400): {bad}"


def test_version_synced_everywhere():
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    m = re.search(r'^version\s*=\s*"([^"]+)"', text, re.M)
    assert m, "no version in pyproject.toml"
    version = m.group(1)
    import ikarem

    assert ikarem.__version__ == version, f"ikarem.__version__={ikarem.__version__} != {version}"
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert f"## [{version}]" in changelog, f"CHANGELOG has no ## [{version}] section"
