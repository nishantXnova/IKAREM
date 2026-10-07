"""Devtools that earn trust: check audits what the interpreter accepts.

The centerpiece: an f-string (or .format/%) query inside a db call runs
fine and passes tests on benign data — then injects in production.
`ikarem check` flags it statically, with the remedy attached.
"""

import pytest

from ikarem import Blueprint, Ikarem, MethodView
from ikarem.audit import audit_handler
from ikarem.compiled import check_app


def _route(app, path="/x"):
    def deco(fn):
        app.router.add(path, {"GET"}, fn, getattr(fn, "__name__", "h"))
        return fn

    return deco


def test_fstring_query_flagged_with_remedy():
    async def h(req, db=None):
        return await db.execute(f"SELECT * FROM t WHERE id = {1}")

    findings = audit_handler(h)
    assert len(findings) == 1
    assert "f-string" in findings[0] and "?" in findings[0]


def test_format_and_percent_forms_flagged():
    async def h1(req, db=None):
        return await db.fetch_all("SELECT * FROM t WHERE id = {}".format(1))

    async def h2(req, db=None):
        return await db.fetch_one("SELECT * FROM t WHERE id = %s" % 1)

    assert any(".format()" in f for f in audit_handler(h1))
    assert any("%-formatting" in f for f in audit_handler(h2))


def test_safe_queries_silent():
    async def placeholders(req, db=None):
        return await db.execute("SELECT * FROM t WHERE id = ?", 1)

    async def static_fstring(req, db=None):
        return await db.execute("SELECT version FROM schema_migrations")

    async def plain(req):
        return {"ok": True}

    assert audit_handler(placeholders) == []
    assert audit_handler(static_fstring) == []
    assert audit_handler(plain) == []


def test_identifier_interpolation_still_flagged_but_allowlisted():
    # The 1%: table names can't use ? — the message must say allowlist,
    # and the finding stays a warning (never an error) for this reason.
    async def h(req, db=None):
        table = "tasks"
        assert table in ("tasks", "notes")
        return await db.execute(f"DELETE FROM {table} WHERE id = ?", 1)

    (finding,) = audit_handler(h)
    assert "allowlist" in finding


def test_blocking_calls_flagged():
    import time

    async def sleeper(req):
        time.sleep(1)
        return {"ok": True}

    async def fetcher(req):
        import requests

        return {"ok": requests.get("http://x").status_code}

    async def clean(req):
        import asyncio

        await asyncio.sleep(0)
        return {"ok": True}

    assert any("time.sleep" in f for f in audit_handler(sleeper))
    assert any("requests.get" in f for f in audit_handler(fetcher))
    assert audit_handler(clean) == []


def test_blueprint_and_methodview_handlers_audited():
    bp = Blueprint("b", url_prefix="/b")

    @bp.get("/q")
    async def q(req, db=None):
        return await db.execute(f"SELECT {1}")

    class V(MethodView):
        async def get(self, req, db=None):
            return await db.fetch_all(f"SELECT {1}")

    assert audit_handler(q) != []  # unwrapped through functools.wraps
    view = V.as_view("v")
    assert any("method get" in f for f in audit_handler(view))


def test_unauditable_handlers_never_crash():
    # Whatever getsource/unwrap do here, audit must return a list, not raise.
    assert isinstance(audit_handler(lambda req: {"ok": True}), list)
    assert isinstance(audit_handler(object()), list)
    assert isinstance(audit_handler(print), list)


def test_check_app_surfaces_audit_as_warnings_not_errors():
    app = Ikarem(enable_docs=False)

    @_route(app, path="/bad")
    async def bad(req, db=None):
        return await db.execute(f"SELECT {1}")

    @_route(app, path="/good")
    async def good(req):
        return {"ok": True}

    report = check_app(app)
    assert report["errors"] == []
    assert any("f-string" in w and "/bad" in w for w in report["warnings"])
    assert not any("/good" in w for w in report["warnings"])


def test_exposure_audit_flags_public_writes_only():
    from ikarem import Depends, require_roles

    app = Ikarem(enable_docs=False, auth_secret="devtools-secret")

    @app.post("/public-write")
    async def pub(req):
        return {"ok": True}

    @app.post("/guarded")
    async def guarded(req, claims=Depends(require_roles("admin"))):
        return {"ok": True}

    @app.get("/reads-are-fine")
    async def reads(req):
        return {"ok": True}

    report = check_app(app)
    assert report["errors"] == []
    assert any(
        "/public-write" in w and "no bearer/API-key guard" in w and "require_roles" in w
        for w in report["warnings"]
    )
    assert not any("/guarded" in w or "/reads-are-fine" in w for w in report["warnings"])


def _run_cli(monkeypatch, capsys, *argv):
    import tests.test_devtools as selfmod
    from ikarem import cli

    selfmod._app = _cli_app()
    monkeypatch.setattr("sys.argv", ["ikarem", *argv])
    with pytest.raises(SystemExit) as e:
        cli.main()
    return e.value.code, capsys.readouterr().out


def _cli_app():
    app = Ikarem(enable_docs=False)

    @_route(app, path="/wobbly")
    async def wobbly(req, db=None):
        return await db.execute(f"SELECT {1}")

    return app


def test_check_strict_and_json(monkeypatch, capsys):
    import json

    code, out = _run_cli(monkeypatch, capsys, "check", "tests.test_devtools:_app")
    assert code == 0 and "check OK" in out  # warnings don't fail by default
    code, out = _run_cli(monkeypatch, capsys, "check", "tests.test_devtools:_app", "--strict")
    assert code == 1 and "strict" in out
    code, out = _run_cli(monkeypatch, capsys, "check", "tests.test_devtools:_app", "--format", "json")
    assert code == 0
    report = json.loads(out)
    assert any("f-string" in w for w in report["warnings"])


def test_inspect_openapi_and_auth(monkeypatch, capsys):
    import json

    from ikarem import Depends, require_roles

    app = Ikarem(enable_docs=False, auth_secret="devtools-secret")

    @app.get("/pub")
    async def pub(req):
        return {"ok": True}

    @app.get("/adm")
    async def adm(req, claims=Depends(require_roles("admin"))):
        return {"ok": True}

    import tests.test_devtools as selfmod
    from ikarem import cli

    selfmod._app = app
    monkeypatch.setattr("sys.argv", ["ikarem", "inspect", "tests.test_devtools:_app", "--format", "openapi"])
    with pytest.raises(SystemExit) as e:
        cli.main()
    assert e.value.code == 0
    spec = json.loads(capsys.readouterr().out)
    assert spec["openapi"].startswith("3.1") and "/pub" in spec["paths"]

    monkeypatch.setattr("sys.argv", ["ikarem", "inspect", "tests.test_devtools:_app", "--format", "auth"])
    with pytest.raises(SystemExit) as e:
        cli.main()
    out = capsys.readouterr().out
    assert "[public]" in out and "[auth:bearer" in out


def test_mcp_list(monkeypatch, capsys):
    import tests.test_devtools as selfmod
    from ikarem import cli

    selfmod._app = _cli_app()
    monkeypatch.setattr("sys.argv", ["ikarem", "mcp", "tests.test_devtools:_app", "--list"])
    with pytest.raises(SystemExit) as e:
        cli.main()
    assert e.value.code == 0
    assert "wobbly" in capsys.readouterr().out


_app = None  # placeholder so `tests.test_devtools:_app` resolves if referenced
