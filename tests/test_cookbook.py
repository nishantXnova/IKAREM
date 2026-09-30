"""Cookbook that runs: every docs/COOKBOOK.md python block executes in CI.

If this test breaks, a recipe drifted — fix the recipe, not (only) the test.
"""

import pathlib
import re

import pytest

COOKBOOK = pathlib.Path(__file__).parent.parent / "docs" / "COOKBOOK.md"

EXPECTED_RECIPES = 20


def _blocks():
    text = COOKBOOK.read_text(encoding="utf-8")
    return re.findall(r"```python\n(.*?)```", text, re.S)


def test_cookbook_has_all_recipes():
    assert COOKBOOK.exists(), "docs/COOKBOOK.md missing"
    assert len(_blocks()) == EXPECTED_RECIPES, f"expected {EXPECTED_RECIPES} recipes"


@pytest.mark.parametrize("i", list(range(EXPECTED_RECIPES)))
def test_cookbook_recipe_executes(i):
    blocks = _blocks()
    assert len(blocks) == EXPECTED_RECIPES, "recipe count drifted"
    ns: dict = {"__name__": f"cookbook_recipe_{i}"}
    exec(compile(blocks[i], f"<cookbook-{i}>", "exec"), ns)


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
