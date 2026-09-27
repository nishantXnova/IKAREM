"""Docs that run: README python snippets execute against TestClient.

If this test breaks, the docs drifted — fix the docs, not (only) the test.
"""

import pathlib
import re

import pytest

README = pathlib.Path(__file__).parent.parent / "README.md"


def _snippets():
    text = README.read_text(encoding="utf-8")
    return re.findall(r"```python\n(.*?)```", text, re.S)


def test_readme_snippets_execute():
    from ikarem.testing import TestClient

    runnable = [
        s for s in _snippets() if ("@app.get" in s or "@app.post" in s) and re.search(r"^app\s*=", s, re.M)
    ]
    assert len(runnable) >= 2, "README lost its runnable examples"
    for i, code in enumerate(runnable):
        ns: dict = {"__name__": f"readme_snippet_{i}"}
        exec(compile(code, f"<readme-{i}>", "exec"), ns)
        app = ns["app"]
        c = TestClient(app)
        if '@app.get("/")' in code:
            assert c.get("/").status_code == 200, f"snippet {i}: GET / failed"


def test_readme_industry_snippet_end_to_end():
    from ikarem.testing import TestClient

    runnable = [s for s in _snippets() if "BackgroundTasks" in s]
    assert runnable, "industry example missing from README"
    ns: dict = {"__name__": "readme_industry"}
    exec(compile(runnable[0], "<readme-industry>", "exec"), ns)
    app, tok = ns["app"], ns["tok"]
    c = TestClient(app)
    r = c.post("/items", body={"name": "apple", "qty": "2"})
    assert r.status_code == 200, r.text
    assert r.json() == {"msg": "hi apple", "qty": 2}
    assert c.get("/admin").status_code == 401
    admin = c.get("/admin", headers={"authorization": f"Bearer {tok}"})
    assert admin.json() == {"sub": "u1"}
    assert "pytest -q" in README.read_text(encoding="utf-8")  # verification docs present


def test_migration_guide_diff_is_real():
    text = (pathlib.Path(__file__).parent.parent / "docs" / "MIGRATING_FROM_MERAKI.md").read_text(
        encoding="utf-8"
    )
    assert "from ikarem.meraki_compat import Meraki" in text
    # the exact advertised diff, executed:
    from ikarem.meraki_compat import Meraki
    from ikarem.testing import TestClient as TC

    app = Meraki()

    @app.get("/")
    async def home(request):
        from ikarem.meraki_compat import MerakiResponse

        return MerakiResponse(body=b"migrated")

    assert TC(app).get("/").text == "migrated"


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
