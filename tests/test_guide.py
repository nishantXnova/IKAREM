"""Guide that runs: every docs/GUIDE.md python block executes in CI.

If this test breaks, a chapter drifted — fix the chapter, not (only) the test.
"""

import pathlib
import re

import pytest

GUIDE = pathlib.Path(__file__).parent.parent / "docs" / "GUIDE.md"

EXPECTED_CHAPTERS = 25


def _blocks():
    text = GUIDE.read_text(encoding="utf-8")
    return re.findall(r"```python\n(.*?)```", text, re.S)


def test_guide_has_all_chapters():
    assert GUIDE.exists(), "docs/GUIDE.md missing"
    assert len(_blocks()) == EXPECTED_CHAPTERS, f"expected {EXPECTED_CHAPTERS} chapters"


@pytest.mark.parametrize("i", list(range(EXPECTED_CHAPTERS)))
def test_guide_chapter_executes(i):
    blocks = _blocks()
    assert len(blocks) == EXPECTED_CHAPTERS, "chapter count drifted"
    ns: dict = {"__name__": f"guide_chapter_{i}"}
    exec(compile(blocks[i], f"<guide-{i}>", "exec"), ns)


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
