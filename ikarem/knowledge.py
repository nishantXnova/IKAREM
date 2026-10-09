"""IKAREM framework knowledge as MCP tools + prompts. Stdlib only.

``ikarem mcp ikarem.knowledge:app`` — teaches agents to write IKAREM code:
API reference straight from the source, docs search across the repo,
runnable examples, an embedded cheat-sheet that works even without the
repo checkout, and an audit tool that runs the framework's own
``check`` against candidate code.

Docs resolve relative to the installed package (repo root in an
editable install). When the checkout is absent, doc tools say exactly
how to fix it and ``ikarem_quickref`` still answers.
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path

import ikarem
from ikarem import Ikarem

app = Ikarem(version=getattr(ikarem, "__version__", "0.0.0"))

ROOT = Path(ikarem.__file__).resolve().parent.parent
DOCS = ROOT / "docs"
SITE = ROOT / "site"
EXAMPLES = ROOT / "examples"
SRC = ROOT / "ikarem"

TOPICS = {
    "manual": SITE / "llms.txt",
    "guide": DOCS / "GUIDE.md",
    "cookbook": DOCS / "COOKBOOK.md",
    "nish": DOCS / "NISH.md",
    "deploy": DOCS / "DEPLOY.md",
    "plugins": DOCS / "PLUGINS.md",
    "defaults": DOCS / "DEFAULTS.md",
    "ecosystem": DOCS / "ECOSYSTEM.md",
    "phase1": DOCS / "PHASE1.md",
    "migrating_fastapi": DOCS / "MIGRATING_FROM_FASTAPI.md",
    "migrating_starlette": DOCS / "MIGRATING_FROM_STARLETTE.md",
    "migrating_litestar": DOCS / "MIGRATING_FROM_LITESTAR.md",
    "migrating_flask": DOCS / "MIGRATING_FROM_FLASK.md",
    "migrating_django": DOCS / "MIGRATING_FROM_DJANGO.md",
    "migrating_meraki": DOCS / "MIGRATING_FROM_MERAKI.md",
}

CAP = 6000


def _fix() -> str:
    return (
        "IKAREM repo docs not found next to the installed package "
        f"(looked in {DOCS}). Clone the repo for full docs: "
        "git clone https://github.com/nishantXnova/IKAREM "
        "— or use ikarem_quickref, which needs no checkout."
    )


def _read(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        return f"ERROR: cannot read {path.name}: {e}"
    if len(text) > CAP:
        return text[:CAP] + f"\n\n…({len(text) - CAP} more chars in {path})"
    return text


QUICKREF = """IKAREM quickref — zero-dependency Python ASGI (pip install ikarem; serve: pip install ikarem[server]).

from ikarem import Ikarem
app = Ikarem(debug=True)  # enable_docs=False hides /openapi.json + /docs, never /healthz

@app.get("/users/{uid:int}")     # get/post/put/patch/delete; converters str/int/float/uuid/path
async def get_user(req, uid: int):   # req FIRST, always — path params resolve by name
    return {"uid": uid}          # dict/list/str/bytes auto-respond; (body, status) sets status

Bodies: await req.json() / req.form() / req.body() / req.text(); query: req.query (dict).
Validation: class Item(Schema): name: str; qty: int = 1 → handler(req, item: Item) coerces, errors → 400.
Field: Field(..., min_length=8, email=True). Extra: Schema(extra="forbid").
DI: def dep(): ... → handler(req, x=Depends(dep)); nesting, per-request cache, sync/async/yield all fine.
Auth: app = Ikarem(auth_secret="s"); create_token("u1","s",roles=["admin"]); claims=Depends(require_roles("admin")).
Responses: JSONResponse/TextResponse/HTMLResponse/RedirectResponse/FileResponse/XMLResponse/StreamingResponse.
Errors: abort(403, "owner only"); @app.exception_handler(ExcType). HTTPException hierarchy.
Middleware: app.use(CORSMiddleware()); RateLimit/SecurityHeaders/TrustedHost/Timeout/ConcurrencyLimit/Idempotency.
Static: app.mount_static("/s", "dir"). WebSocket: @app.websocket("/ws") + Room pub/sub.
Background: param bg: BackgroundTasks → bg.add(fn, *a). Cron: @app.every(300)/@app.cron("0 2 * * *") + await app.start_scheduler().
DB: DatabasePlugin("sqlite://...") / postgres/mysql/sqlserver URLs; Migrator + `ikarem migrate`.
MCP: @app.tool("name") plain fn (Schema params validate); @app.prompt("name") message template.
Testing: from ikarem.testing import TestClient; c = TestClient(app); c.get/post/put/patch/delete; c.ws_connect.
Audit: ikarem check myapp:app (AST: f-string SQL + blocking calls warn). Serve: app.run() / ikarem run myapp:app.
Docs: GET /healthz /readyz /metrics /openapi.json /docs free on every app. Laws: stdlib-only core, every behavior tested, no per-request reflection, errors say how to fix.
"""


@app.tool("ikarem_quickref")
def quickref() -> str:
    """The IKAREM cheat-sheet: routing, bodies, Schema, DI, auth, responses, testing, CLI. Needs no repo checkout."""
    return QUICKREF


@app.tool("ikarem_doc")
def doc(topic: str = "manual") -> str:
    """Read a framework doc: manual, guide, cookbook, nish, deploy, plugins, defaults, ecosystem, phase1, migrating_<fastapi|starlette|litestar|flask|django|meraki>."""
    key = (topic or "").strip().lower().replace("-", "_").replace(" ", "_")
    if key in ("index", "topics", "list", ""):
        return "Topics: " + ", ".join(sorted(TOPICS))
    if key not in TOPICS:
        close = [t for t in TOPICS if key in t or t in key] or sorted(TOPICS)
        raise ValueError(f"unknown topic '{topic}'. Did you mean: {', '.join(close[:5])}?")
    path = TOPICS[key]
    if not path.exists():
        raise ValueError(_fix())
    return f"--- {path.name} ---\n" + _read(path)


@app.tool("ikarem_search")
def search(query: str, where: str = "all") -> str:
    """Grep the framework: where = all|src|docs|examples. Returns file:line matches (max 30)."""
    q = (query or "").strip()
    if not q:
        raise ValueError("query is empty — try a symbol like 'Depends' or a concept like 'rate limit'")
    roots = {"src": [SRC], "docs": [DOCS], "examples": [EXAMPLES]}
    dirs = roots.get((where or "all").lower(), [SRC, DOCS, EXAMPLES])
    if not SRC.exists():
        raise ValueError(_fix())
    hits: list[str] = []
    for d in dirs:
        if not d.exists():
            continue
        for p in sorted(d.rglob("*.py" if d == SRC else "*.md")):
            if "__pycache__" in p.parts:
                continue
            try:
                lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            for i, line in enumerate(lines, 1):
                if q.lower() in line.lower():
                    hits.append(f"{p.relative_to(ROOT)}:{i}: {line.strip()[:160]}")
                    if len(hits) >= 30:
                        return "\n".join(hits) + "\n…(capped at 30 — narrow the query)"
    return "\n".join(hits) if hits else f"(no matches for '{q}' in {where})"


@app.tool("ikarem_api")
def api(symbol: str) -> str:
    """API reference for an ikarem.* symbol: signature + docstring (e.g. Ikarem, Schema, Depends, TestClient)."""
    name = (symbol or "").strip()
    if not name:
        raise ValueError(
            "symbol is empty. Try: Ikarem, Schema, Field, Depends, Request, TestClient, Migrator"
        )
    try:
        obj = getattr(sys.modules.get("ikarem") or __import__("ikarem"), name)
    except (ImportError, AttributeError):
        names = sorted(n for n in getattr(ikarem, "__all__", []) if name.lower() in n.lower())
        raise ValueError(
            f"no ikarem.{name}."
            + (
                f" Did you mean: {', '.join(names[:5])}?"
                if names
                else " See ikarem_quickref for the export list."
            )
        )
    try:
        sig = str(inspect.signature(obj))
    except (ValueError, TypeError):
        sig = ""
    doc = (inspect.getdoc(obj) or "").strip().splitlines()[:20]
    head = f"ikarem.{name}{sig}"
    if inspect.isclass(obj):
        meths = [m for m, _ in inspect.getmembers(obj, inspect.isfunction) if not m.startswith("_")][:20]
        if meths:
            doc.append(f"\nMethods: {', '.join(meths)}")
    return head + ("\n" + "\n".join(doc) if doc else "")


@app.tool("ikarem_example")
def example(name: str = "") -> str:
    """List runnable examples, or return one by name (e.g. basic)."""
    if not EXAMPLES.exists():
        raise ValueError(_fix())
    files = sorted(p for p in EXAMPLES.glob("*.py") if p.name != "__init__.py")
    key = (name or "").strip().lower()
    if not key:
        return "Examples: " + ", ".join(p.stem for p in files)
    match = next((p for p in files if p.stem == key or key in p.stem), None)
    if match is None:
        raise ValueError(f"no example '{name}'. Available: " + ", ".join(p.stem for p in files))
    return f"--- examples/{match.name} ---\n" + _read(match)


@app.tool("ikarem_audit")
def audit(code: str, app_attr: str = "app") -> str:
    """Run the framework's own check against candidate app code. Returns routes + errors + warnings."""
    if not (code or "").strip():
        raise ValueError("code is empty — pass the candidate app source")
    if len(code) > 60000:
        raise ValueError("code exceeds 60000 chars — split it first")
    import importlib.util
    import tempfile

    with tempfile.TemporaryDirectory(prefix="ikarem_audit_") as tmp:
        mod_path = str(Path(tmp) / "candidate_app.py")
        Path(mod_path).write_text(code, encoding="utf-8")
        try:
            spec = importlib.util.spec_from_file_location("candidate_app", mod_path)
            mod = importlib.util.module_from_spec(spec)
            sys.modules["candidate_app"] = mod
            spec.loader.exec_module(mod)
            target = getattr(mod, (app_attr or "app").strip() or "app", None)
            if target is None:
                cands = [n for n in vars(mod) if isinstance(getattr(mod, n), Ikarem)]
                raise ValueError(
                    f"no Ikarem app as '{app_attr}'."
                    + (f" Found app(s): {', '.join(cands)} — retry with app_attr set." if cands else "")
                )
            from ikarem.compiled import check_app

            target.compile_all()
            report = check_app(target)
        except Exception:
            raise  # candidate crashed before audit — surfaces as isError naming the failure
        finally:
            sys.modules.pop("candidate_app", None)
    lines = [f"routes: {len(report.get('routes', []))}"]
    for r in report.get("routes", [])[:20]:
        lines.append(f"  {'/'.join(sorted(r['methods']))} {r['path']} -> {r['handler']}")
    for w in report.get("warnings", []):
        lines.append(f"WARN: {w}")
    for e in report.get("errors", []):
        lines.append(f"ERROR: {e}")
    if not report.get("errors") and not report.get("warnings"):
        lines.append("check OK — ships it")
    return "\n".join(lines)


@app.prompt("ikarem_new_app")
def prompt_new_app(name: str, kind: str = "api") -> str:
    """Scaffold a new IKAREM app the framework-native way: TestClient-first, check-gated."""
    return (
        f"Build a new IKAREM {kind} app named '{name}'. Follow this loop:\n"
        "1. Read ikarem_quickref, then the manual (ikarem_doc manual) for anything it doesn't cover.\n"
        "2. Look at ikarem_example basic for the minimal shape. Handlers take req FIRST.\n"
        "3. Write the app, then immediately drive it with TestClient (from ikarem.testing import TestClient) "
        "— no server needed. Assert every route, including 404 vs 405.\n"
        "4. Run ikarem_audit on the finished code; fix every ERROR and WARN before FINAL.\n"
        "5. Laws: stdlib-only imports in app code, every behavior gets a test, errors must say how to fix.\n"
        "Serve only when asked: app.run() needs pip install ikarem[server]."
    )


@app.prompt("ikarem_review")
def prompt_review(code: str) -> str:
    """Review IKAREM code against the framework laws. Paste the code as the argument."""
    return (
        "Review this IKAREM code against the five laws:\n"
        "1. Zero-dep core: any non-stdlib import in app code? Name it and the extra that provides it.\n"
        "2. Tests: is every behavior asserted via TestClient? List what's uncovered.\n"
        "3. No per-request reflection: any signature inspection or get_type_hints in the hot path?\n"
        "4. Errors: do messages say how to fix (pip install …, pass auth_secret=, Did you mean)? Quote failures.\n"
        "5. Handlers take req first; dict/list/str/bytes auto-respond; converters 404 instead of crashing.\n"
        "Verdict: ship / fix-first with a checklist. Cite line content for every claim.\n"
        f"CODE:\n{code}"
    )
