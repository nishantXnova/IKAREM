"""Migration guides that run: each guide's IKAREM landing snippet executes.

Guides use bare ``` fences for source-framework sketches (never executed)
and exactly one ```python block: the runnable IKAREM landing snippet.
"""

import pathlib
import re

import pytest

GUIDES = [
    "MIGRATING_FROM_FASTAPI.md",
    "MIGRATING_FROM_LITESTAR.md",
    "MIGRATING_FROM_STARLETTE.md",
    "MIGRATING_FROM_FLASK.md",
    "MIGRATING_FROM_DJANGO.md",
]

DOCS = pathlib.Path(__file__).parent.parent / "docs"


def _python_blocks(name: str):
    text = (DOCS / name).read_text(encoding="utf-8")
    return re.findall(r"```python\n(.*?)```", text, re.S)


@pytest.mark.parametrize("guide", GUIDES)
def test_guide_has_exactly_one_runnable_block(guide):
    assert (DOCS / guide).exists(), f"docs/{guide} missing"
    assert len(_python_blocks(guide)) == 1, f"{guide} must keep exactly one runnable block"


@pytest.mark.parametrize("guide", GUIDES)
def test_guide_landing_snippet_executes(guide):
    (block,) = _python_blocks(guide)
    ns: dict = {"__name__": f"migrate_{guide.lower().replace('.md', '')}"}
    exec(compile(block, f"<{guide}>", "exec"), ns)


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
