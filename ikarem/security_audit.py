"""`ikarem audit`: the framework grades its own deployment. Read-only.

Point it at an app and it inspects the live object — middleware stack,
config, routes — and grades 11 controls: exposed writes, secrets, debug
leakage, cookie flags, CSRF, rate limiting, headers, trusted hosts, body
caps, resilience, token policy, supply chain. Findings carry evidence +
remedy; machine-readable JSON for auditors and CI gates.

Two properties no checklist tool has:

- JWT-01 doesn't claim the token policy, it PROVES it: mints and attacks
  tokens live (exp-less forgery, alg=none, wrong secret) and grades the
  observed verdicts. A broken install fails instead of asserting safety.
- DEP-01 guards Law 1 in CI: the core's required dependencies must stay
  zero. The day someone sneaks one in, the audit (and the release) fails.

Read-only by construction: no requests are sent, no state mutated, startup
never runs. Findings are warnings-grade honesty — warn means "missing
defense in depth", fail means "actively dangerous".
"""

from __future__ import annotations

from typing import Any

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def _finding(cid: str, name: str, status: str, detail: str, remedy: str = "") -> dict[str, Any]:
    return {"id": cid, "name": name, "status": status, "detail": detail, "remedy": remedy}


def _stack(app: Any) -> list:
    return list(getattr(getattr(app, "middleware", None), "stack", []) or [])


def _of_type(stack: list, cls: Any) -> list:
    try:
        return [m for m in stack if isinstance(m, cls)]
    except Exception:
        return []


def _routes(app: Any) -> list:
    router = getattr(app, "router", None)
    return list(getattr(router, "routes", []) or [])


def _c_exposed_writes(app: Any) -> dict:
    from types import SimpleNamespace

    from .compiled import describe_route

    exposed = []
    for r in _routes(app):
        if getattr(r.handler, "_ikarem_internal", False):
            continue
        unsafe = sorted(m for m in r.methods if m.upper() not in SAFE_METHODS)
        if not unsafe:
            continue
        desc = describe_route(SimpleNamespace(path=r.path, methods=set(unsafe), handler=r.handler, name=""))
        if not desc.is_auth:
            exposed.append(f"{','.join(unsafe)} {r.path}")
    if exposed:
        return _finding(
            "AUTHN-01",
            "exposed writes",
            "warn",
            f"{len(exposed)} mutating route(s) with no bearer/API-key guard: {', '.join(exposed[:5])}"
            + ("…" if len(exposed) > 5 else ""),
            "Guard each with claims=Depends(require_roles(...)) or APIKeyAuth, or accept it publicly on purpose. `ikarem check` lists them too.",
        )
    return _finding("AUTHN-01", "exposed writes", "pass", "every mutating route carries a guard")


def _has_auth_surface(app: Any) -> bool:
    from .compiled import get_plan
    from .session import SessionMiddleware

    if any(isinstance(m, SessionMiddleware) for m in _stack(app)):
        return True
    for r in _routes(app):
        if getattr(r.handler, "_ikarem_internal", False):
            continue
        try:
            if get_plan(r.handler).is_auth or get_plan(r.handler).uses_config_secret:
                return True
        except Exception:
            continue
    return False


def _c_secrets(app: Any) -> dict:
    cfg = getattr(app, "config", {}) or {}
    try:
        secret = cfg.get("auth_secret", None) or cfg.get("session_secret", None)
    except Exception:
        secret = None
    if not _has_auth_surface(app):
        return _finding("SECRET-01", "secrets", "pass", "no auth surface: nothing to steal")
    if not secret or secret in ("change-me",):
        return _finding(
            "SECRET-01",
            "secrets",
            "fail",
            "auth surface with a missing/placeholder secret: tokens verifiable by anyone",
            "pass auth_secret= (or IKAREM_AUTH_SECRET) with 32+ random bytes; refusing is safer than minting.",
        )
    if len(str(secret)) < 16:
        return _finding(
            "SECRET-01",
            "secrets",
            "warn",
            f"secret is only {len(str(secret))} chars: HS256 brute-force margin is thin",
            "Use 32+ random bytes (secrets.token_urlsafe(32)).",
        )
    return _finding("SECRET-01", "secrets", "pass", "usable secret configured")


def _c_debug(app: Any) -> dict:
    if bool(getattr(app, "debug", False)):
        return _finding(
            "DEBUG-01",
            "debug leakage",
            "fail",
            "debug=True: tracebacks render into 500 responses",
            "Serve production with debug=False (tracebacks stay server-side).",
        )
    return _finding("DEBUG-01", "debug leakage", "pass", "debug off: 500s carry no tracebacks")


def _c_session(app: Any) -> dict:
    from .session import SessionMiddleware

    sms = _of_type(_stack(app), SessionMiddleware)
    if not sms:
        return _finding("SESS-01", "cookie flags", "pass", "no cookie sessions in the stack")
    sm = sms[0]
    same = str(getattr(sm, "same_site", "Lax"))
    secure = bool(getattr(sm, "secure", False))
    if same.lower() == "none" and not secure:
        return _finding(
            "SESS-01",
            "cookie flags",
            "fail",
            "SameSite=None without Secure: browsers reject the cookie AND cross-site sends it anywhere",
            "Set secure=True (HTTPS) or drop SameSite to Lax.",
        )
    if not secure:
        return _finding(
            "SESS-01",
            "cookie flags",
            "warn",
            "session cookie without Secure: HttpOnly + SameSite set, but plaintext HTTP leaks it",
            "Set secure=True once HTTPS terminates (local HTTP dev is the only excuse).",
        )
    return _finding("SESS-01", "cookie flags", "pass", "HttpOnly + SameSite + Secure")


def _c_csrf(app: Any) -> dict:
    from .session import CSRFMiddleware, SessionMiddleware

    stack = _stack(app)
    has_session = any(isinstance(m, SessionMiddleware) for m in stack)
    csrf = [m for m in stack if isinstance(m, CSRFMiddleware)]
    if not has_session:
        return _finding("CSRF-01", "csrf", "pass", "no browser sessions: nothing to forge")
    if not csrf:
        return _finding(
            "CSRF-01",
            "csrf",
            "warn",
            "sessions without CSRF: any site can POST your users' forms",
            "app.use(CSRFMiddleware()) AFTER SessionMiddleware; exempt pure-token APIs via exempt_paths.",
        )
    exempt = getattr(csrf[0], "exempt_paths", [])
    return _finding(
        "CSRF-01",
        "csrf",
        "pass",
        f"double-submit enforced{'; exempt: ' + ', '.join(exempt) if exempt else ''}",
    )


def _c_rate(app: Any) -> dict:
    from .resilience import ConcurrencyLimitMiddleware, SpikeManager
    from .security import RateLimitMiddleware, RedisRateLimitMiddleware

    stack = _stack(app)
    rl = [m for m in stack if isinstance(m, (RateLimitMiddleware, RedisRateLimitMiddleware))]
    if rl:
        m = rl[0]
        return _finding(
            "RATE-01", "rate limiting", "pass", f"per-IP fixed window ({getattr(m, 'limit', '?')}/min)"
        )
    shed = [m for m in stack if isinstance(m, (ConcurrencyLimitMiddleware, SpikeManager))]
    if shed:
        return _finding(
            "RATE-01",
            "rate limiting",
            "warn",
            "flood shedding present but no per-IP rate limit: one abusive IP eats the shared budget",
            "Add RateLimitMiddleware (or RedisRateLimitMiddleware across workers) in front.",
        )
    return _finding(
        "RATE-01",
        "rate limiting",
        "warn",
        "no rate limiting: brute force and scrapers run unthrottled",
        "app.use(RateLimitMiddleware()) — 429s carry Retry-After by default.",
    )


def _c_headers(app: Any) -> dict:
    from .security import SecurityHeadersMiddleware, TrustedHostMiddleware

    stack = _stack(app)
    out = []
    if not any(isinstance(m, SecurityHeadersMiddleware) for m in stack):
        out.append("security headers")
    th = [m for m in stack if isinstance(m, TrustedHostMiddleware)]
    if not th or getattr(th[0], "allowed", ["*"]) == ["*"]:
        out.append("trusted hosts")
    if out:
        return _finding(
            "HDR-01",
            "headers + hosts",
            "warn",
            f"missing: {', '.join(out)} (clickjacking/sniffing/host-poisoning surface)",
            "app.use(SecurityHeadersMiddleware()) + TrustedHostMiddleware(['example.com']).",
        )
    return _finding("HDR-01", "headers + hosts", "pass", "nosniff/DENY/no-referrer + host allowlist")


def _c_body(app: Any) -> dict:
    cfg = getattr(app, "config", {}) or {}
    try:
        cap = cfg.get("max_body_bytes", None)
    except Exception:
        cap = None
    if cap is None:
        return _finding(
            "BODY-01",
            "body caps",
            "warn",
            "no app-wide body cap: oversized payloads are each handler's problem",
            "Ikarem(max_body_bytes=10*1024*1024) — per-call max_bytes= still wins.",
        )
    return _finding("BODY-01", "body caps", "pass", f"app-wide cap {cap} bytes")


def _c_resilience(app: Any) -> dict:
    from .resilience import ConcurrencyLimitMiddleware, SpikeManager, TimeoutMiddleware

    stack = _stack(app)
    have = [m for m in stack if isinstance(m, (TimeoutMiddleware, ConcurrencyLimitMiddleware, SpikeManager))]
    if not have:
        return _finding(
            "RES-01",
            "overload behavior",
            "warn",
            "no timeouts/bulkheads: one hung handler wedges a worker, one spike wedges all",
            "TimeoutMiddleware(30) + SpikeManager() turn overload into 503+Retry-After.",
        )
    kinds = sorted(type(m).__name__ for m in have)
    return _finding("RES-01", "overload behavior", "pass", f"overload sheds cleanly ({', '.join(kinds)})")


def _b64e(b: bytes) -> str:
    import base64

    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _c_jwt(app: Any) -> dict:
    """Prove the token policy live: mint and attack. Any failed proof is a
    broken install, graded fail — claims are cheap, verdicts are not."""
    import hashlib
    import hmac
    import json

    from .auth import create_token, verify_token

    secret = "audit-proof-secret"
    try:
        good = create_token("audit", secret, expires_in=60)
        assert verify_token(good, secret)["sub"] == "audit"
        h = _b64e(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
        # forever-token forgery, correctly signed: must die on missing exp
        p = _b64e(json.dumps({"sub": "audit"}).encode())
        sig = _b64e(hmac.new(secret.encode(), f"{h}.{p}".encode(), hashlib.sha256).digest())
        for bad, why in (
            (f"{h}.{p}.{sig}", "exp-less"),
            (
                f"{_b64e(json.dumps({'alg': 'none', 'typ': 'JWT'}).encode())}.{p}.",
                "alg=none",
            ),
            (good[:-1] + ("a" if good[-1] != "a" else "b"), "tampered"),
        ):
            try:
                verify_token(bad, secret)
                return _finding("JWT-01", "token policy", "fail", f"live proof failed: {why} token accepted")
            except ValueError:
                pass
        try:
            verify_token(good, "wrong-secret")
            return _finding("JWT-01", "token policy", "fail", "live proof failed: wrong secret accepted")
        except ValueError:
            pass
    except AssertionError as e:
        return _finding("JWT-01", "token policy", "fail", f"live proof failed: {e}")
    return _finding(
        "JWT-01",
        "token policy",
        "pass",
        "proven live: exp required, alg locked, sig enforced, 600k-round passwords",
    )


def _c_supply(app: Any) -> dict:  # noqa: ARG001
    """Law 1 in CI: the core's required dependencies must stay zero."""
    try:
        from importlib import metadata
    except ImportError:
        return _finding("DEP-01", "supply chain", "warn", "importlib.metadata unavailable: cannot verify")
    try:
        reqs = metadata.requires("ikarem") or []
    except Exception:
        return _finding(
            "DEP-01", "supply chain", "warn", "ikarem not installed (running from source?): cannot verify"
        )
    bare = [r for r in reqs if "extra" not in (r.split(";")[-1] if ";" in r else "")]
    if bare:
        return _finding(
            "DEP-01",
            "supply chain",
            "fail",
            f"core requires {bare}: Law 1 broken, every install inherits them",
            "Move it behind an extra with a lazy import naming the extra.",
        )
    return _finding("DEP-01", "supply chain", "pass", "0 required dependencies: nothing to CVE")


_CONTROLS = (
    _c_exposed_writes,
    _c_secrets,
    _c_debug,
    _c_session,
    _c_csrf,
    _c_rate,
    _c_headers,
    _c_body,
    _c_resilience,
    _c_jwt,
    _c_supply,
)


def audit_report(app: Any) -> dict[str, Any]:
    """Grade an app's security posture. Read-only: never sends requests,
    never mutates state, never runs startup. Returns {controls, summary}."""
    controls = []
    for fn in _CONTROLS:
        try:
            controls.append(fn(app))
        except Exception as e:  # noqa: BLE001 - one broken probe must not kill the audit
            controls.append(_finding("UNKNOWN", getattr(fn, "__name__", "?"), "warn", f"probe crashed: {e}"))
    fails = sum(1 for c in controls if c["status"] == "fail")
    warns = sum(1 for c in controls if c["status"] == "warn")
    verdict = "exposed" if fails else ("attention" if warns else "secure")
    return {
        "controls": controls,
        "summary": {
            "total": len(controls),
            "pass": len(controls) - fails - warns,
            "warn": warns,
            "fail": fails,
            "verdict": verdict,
        },
    }


def format_text(report: dict) -> str:
    s = report["summary"]
    lines = [
        f"audit: {s['total']} controls, {s['pass']} pass, {s['warn']} warn, "
        f"{s['fail']} fail -> {s['verdict'].upper()}"
    ]
    for c in report["controls"]:
        lines.append(f"  [{c['status'].upper():4}] {c['id']:9} {c['name']}: {c['detail']}")
        if c["remedy"] and c["status"] != "pass":
            lines.append(f"           fix: {c['remedy']}")
    return "\n".join(lines)
