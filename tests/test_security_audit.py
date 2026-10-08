"""The framework grades its own deployment: controls, verdicts, CLI."""

import pytest

from ikarem import (
    CSRFMiddleware,
    Depends,
    Ikarem,
    RateLimitMiddleware,
    SecurityHeadersMiddleware,
    SessionMiddleware,
    SpikeManager,
    TimeoutMiddleware,
    TrustedHostMiddleware,
    audit_report,
    require_roles,
)


def _hardened_app():
    app = Ikarem(
        enable_docs=False,
        auth_secret="audit-grade-secret-32-bytes-long!!",
        session_secret="audit-grade-session-32-bytes-long!",
        max_body_bytes=1024 * 1024,
    )
    app.use(SecurityHeadersMiddleware())
    app.use(TrustedHostMiddleware(["example.com"]))
    app.use(SessionMiddleware(secret="audit-grade-session-32-bytes-long!", secure=True))
    app.use(CSRFMiddleware())
    app.use(RateLimitMiddleware(per_minute=120))
    app.use(TimeoutMiddleware(30))
    app.use(SpikeManager())

    @app.get("/pub")
    async def pub(req):
        return {"ok": True}

    @app.post("/guarded")
    async def guarded(req, claims=Depends(require_roles("admin"))):
        return {"ok": True}

    return app


_audit_app = _hardened_app()


def _statuses(report):
    return {c["id"]: c["status"] for c in report["controls"]}


def test_hardened_app_grades_secure():
    rep = audit_report(_hardened_app())
    assert rep["summary"]["fail"] == 0 and rep["summary"]["warn"] == 0
    assert rep["summary"]["verdict"] == "secure"
    assert rep["summary"]["total"] == 11
    assert all(set(c) == {"id", "name", "status", "detail", "remedy"} for c in rep["controls"])


def test_bare_app_warns_but_never_fails():
    rep = audit_report(Ikarem(enable_docs=False))
    assert rep["summary"] == {
        "total": 11,
        "pass": 7,
        "warn": 4,
        "fail": 0,
        "verdict": "attention",
    }
    st = _statuses(rep)
    assert st["RATE-01"] == st["HDR-01"] == st["BODY-01"] == st["RES-01"] == "warn"
    assert st["SECRET-01"] == "pass"  # no auth surface: nothing to steal
    assert st["JWT-01"] == "pass" and st["DEP-01"] == "pass"  # live proofs + Law 1


def test_placeholder_secret_with_auth_surface_fails():
    app = Ikarem(enable_docs=False)

    @app.post("/pay")
    async def pay(req, claims=Depends(require_roles("admin"))):
        return {"ok": True}

    rep = audit_report(app)
    secret = next(c for c in rep["controls"] if c["id"] == "SECRET-01")
    assert secret["status"] == "fail" and "auth_secret" in secret["remedy"]
    assert rep["summary"]["verdict"] == "exposed"


def test_debug_and_cookie_flags_fail_loudly():
    app = Ikarem(enable_docs=False, debug=True)
    app.use(SessionMiddleware(secret="samesite-test-secret", same_site="None"))
    st = _statuses(audit_report(app))
    assert st["DEBUG-01"] == "fail" and st["SESS-01"] == "fail"

    app2 = Ikarem(enable_docs=False)
    app2.use(SessionMiddleware(secret="samesite-test-secret"))
    st2 = _statuses(audit_report(app2))
    assert st2["SESS-01"] == "warn"  # Lax without Secure: dev-tolerated, flagged


def test_session_without_csrf_warns_with_fix():
    app = Ikarem(enable_docs=False)
    app.use(SessionMiddleware(secret="csrf-test-secret"))
    rep = audit_report(app)
    csrf = next(c for c in rep["controls"] if c["id"] == "CSRF-01")
    assert csrf["status"] == "warn" and "CSRFMiddleware" in csrf["remedy"]


def test_exposed_write_names_the_route():
    app = Ikarem(enable_docs=False)

    @app.post("/transfer")
    async def transfer(req):
        return {"ok": True}

    rep = audit_report(app)
    authn = next(c for c in rep["controls"] if c["id"] == "AUTHN-01")
    assert authn["status"] == "warn" and "POST /transfer" in authn["detail"]


def _run_cli(monkeypatch, capsys, *argv):
    import tests.test_security_audit as selfmod
    from ikarem import cli

    assert hasattr(selfmod, "_audit_app")
    monkeypatch.setattr("sys.argv", ["ikarem", *argv])
    with pytest.raises(SystemExit) as e:
        cli.main()
    return e.value.code, capsys.readouterr().out


def test_cli_text_json_and_strict(monkeypatch, capsys):
    import json

    code, out = _run_cli(monkeypatch, capsys, "audit", "tests.test_security_audit:_audit_app")
    assert code == 0 and "SECURE" in out
    code, out = _run_cli(
        monkeypatch, capsys, "audit", "tests.test_security_audit:_audit_app", "--format", "json"
    )
    assert code == 0
    rep = json.loads(out)
    assert rep["summary"]["verdict"] == "secure" and len(rep["controls"]) == 11
    code, out = _run_cli(monkeypatch, capsys, "audit", "tests.test_security_audit:_audit_app", "--strict")
    assert code == 0  # secure stays green even under strict
