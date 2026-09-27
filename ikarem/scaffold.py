"""`ikarem new <dir>` — production-grade starter: sessions + CSRF + auth +
SQLite CRUD served as HTML forms AND JSON API, with tests + Dockerfile."""

from __future__ import annotations

from pathlib import Path

APP_PY = '''"""Production-grade IKAREM starter: auth + notes, HTML forms + JSON API."""
import secrets

from ikarem import (
    BackgroundTasks, Depends, Field, Ikarem, Schema,
    CORSMiddleware, CSRFMiddleware, RateLimitMiddleware,
    RequestIDMiddleware, SecurityHeadersMiddleware,
    SessionMiddleware, Unauthorized, BadRequest, NotFound,
    check_password, csrf_token, hash_password, verify_token,
)
from ikarem.db import DatabasePlugin
from ikarem.http import HTMLResponse, RedirectResponse

app = Ikarem(session_secret="change-me-in-prod-use-env", db_url="sqlite:///app.db")
app.use(RequestIDMiddleware())
app.use(SecurityHeadersMiddleware())
app.use(CORSMiddleware())
app.use(SessionMiddleware(secure=False))  # secure=True behind HTTPS
app.use(CSRFMiddleware())  # every unsafe route needs the token (headers or form)
app.use(RateLimitMiddleware(per_minute=120))
app.register(DatabasePlugin(app.config.get("db_url", "sqlite:///app.db")))


@app.on_startup
async def init_db():
    db = app.state_db
    await db.execute("CREATE TABLE IF NOT EXISTS users (id TEXT PRIMARY KEY, email TEXT UNIQUE, pw TEXT)")
    await db.execute(
        "CREATE TABLE IF NOT EXISTS notes (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " user_id TEXT, text TEXT, done INTEGER DEFAULT 0)"
    )


class Register(Schema):
    email: str = Field(..., email=True, max_length=254)
    password: str = Field(..., min_length=8, max_length=128)


class NoteIn(Schema):
    text: str = Field(..., min_length=1, max_length=500)


def current_user(req):
    """Session cookie first, Bearer JWT second — same identity either way."""
    uid = req.session.get("uid")
    if uid:
        return uid
    auth = req.headers.get("authorization", "")
    if auth.startswith("Bearer "):
        try:
            return verify_token(auth[7:], req.app.config.get("auth_secret", "change-me"))["sub"]
        except Exception:
            pass
    raise Unauthorized("login required")


def wants_html(req) -> bool:
    return "text/html" in req.headers.get("accept", "")


def page(title, body, user=None) -> HTMLResponse:
    nav = f'<p>hi {user} · <form method="post" action="/logout" style="display:inline"><button>logout</button></form></p>' if user else '<p><a href="/login">login</a> · <a href="/register">register</a></p>'
    return HTMLResponse(f"<!doctype html><html><body><h1>{title}</h1>{nav}{body}</body></html>")


def _form(req, action, fields, submit) -> str:
    inputs = "".join(f'<p>{label}: <input name="{n}"></p>' for n, label in fields)
    return f'<form method="post" action="{action}"><input type="hidden" name="_csrf_token" value="{csrf_token(req)}">{inputs}<button>{submit}</button></form>'


@app.get("/")
async def home(req):
    uid = req.session.get("uid")
    user = None
    if uid:
        row = await req.app.state_db.fetch_one("SELECT email FROM users WHERE id = ?", uid)
        user = row["email"] if row else None
    return page("notes app", '<p><a href="/notes">your notes</a> · <a href="/api/notes">JSON API</a></p>', user)


@app.get("/register")
async def register_page(req):
    return page("register", _form(req, "/register", [("email", "email"), ("password", "password")], "register"))


@app.post("/register")
async def register(req, bg: BackgroundTasks):
    if "application/json" in req.headers.get("content-type", ""):
        data = Register.validate(await req.json())
    else:
        form = await req.form()
        try:
            data = Register.validate({"email": form.get("email", ""), "password": form.get("password", "")})
        except Exception as e:
            raise BadRequest(f"validation failed: {e}")
    uid = secrets.token_hex(8)
    try:
        await req.app.state_db.execute(
            "INSERT INTO users (id, email, pw) VALUES (?, ?, ?)", uid, data.email, hash_password(data.password))
    except Exception:
        raise BadRequest("email already registered")
    req.session["uid"] = uid
    bg.add(print, f"welcome {data.email}")
    if wants_html(req):
        return RedirectResponse("/notes", status_code=303)
    return {"uid": uid, "email": data.email}, 201


@app.get("/login")
async def login_page(req):
    return page("login", _form(req, "/login", [("email", "email"), ("password", "password")], "login"))


@app.post("/login")
async def login(req):
    if "application/json" in req.headers.get("content-type", ""):
        body = await req.json()
        email, pw = body.get("email", ""), body.get("password", "")
    else:
        form = await req.form()
        email, pw = form.get("email", ""), form.get("password", "")
    row = await req.app.state_db.fetch_one("SELECT * FROM users WHERE email = ?", email)
    if not row or not check_password(pw, row["pw"]):
        raise Unauthorized("bad credentials")
    req.session["uid"] = row["id"]
    if wants_html(req):
        return RedirectResponse("/notes", status_code=303)
    return {"uid": row["id"], "email": row["email"]}


@app.post("/logout")
async def logout(req):
    req.session.clear()
    if wants_html(req):
        return RedirectResponse("/", status_code=303)
    return {"ok": True}


@app.get("/notes")
async def notes_page(req, uid=Depends(current_user)):
    rows = await req.app.state_db.fetch_all("SELECT * FROM notes WHERE user_id = ? ORDER BY id DESC", uid)
    items = "".join(f"<li>{r['text']} {'[done]' if r['done'] else ''}</li>" for r in rows) or "<li>none yet</li>"
    return page("your notes", f"<ul>{items}</ul>" + _form(req, "/notes", [("text", "note")], "add"))


@app.post("/notes")
async def notes_add(req, note: NoteIn, uid=Depends(current_user)):
    # NoteIn validates JSON *and* HTML form bodies (see compiled Schema fallback)
    await req.app.state_db.execute("INSERT INTO notes (user_id, text) VALUES (?, ?)", uid, note.text)
    return RedirectResponse("/notes", status_code=303)


@app.get("/api/csrf")
async def api_csrf(req):
    return {"csrf": csrf_token(req)}


@app.get("/api/notes")
async def api_list(req, uid=Depends(current_user)):
    return await req.app.state_db.fetch_all("SELECT id, text, done FROM notes WHERE user_id = ? ORDER BY id DESC", uid)


@app.post("/api/notes")
async def api_create(req, note: NoteIn, uid=Depends(current_user)):
    cur = await req.app.state_db.execute("INSERT INTO notes (user_id, text) VALUES (?, ?)", uid, note.text)
    return {"id": cur, "text": note.text, "done": 0}, 201


@app.put("/api/notes/{nid:int}")
async def api_update(req, nid: int, uid=Depends(current_user)):
    body = await req.json()
    row = await req.app.state_db.fetch_one("SELECT * FROM notes WHERE id = ? AND user_id = ?", nid, uid)
    if not row:
        raise NotFound("note not found")
    await req.app.state_db.execute("UPDATE notes SET text = ?, done = ? WHERE id = ?",
                                   body.get("text", row["text"]), int(bool(body.get("done", row["done"]))), nid)
    return {"id": nid, "ok": True}


@app.delete("/api/notes/{nid:int}")
async def api_delete(req, nid: int, uid=Depends(current_user)):
    await req.app.state_db.execute("DELETE FROM notes WHERE id = ? AND user_id = ?", nid, uid)
    return {"ok": True}
'''

TEST_APP_PY = '''"""Full-stack flow: register -> notes CRUD -> logout, cookies + CSRF."""
import os

os.environ.setdefault("IKAREM_DB_URL", "sqlite:///:memory:")

from app import app
from ikarem.testing import TestClient


def _csrf(c):
    return c.get("/api/csrf").json()["csrf"]


def test_full_flow():
    c = TestClient(app)

    def csrf():
        return {"headers": {"x-csrf-token": _csrf(c)}}

    r = c.post("/register", body={"email": "amy@ex.co", "password": "s3cretpw"}, **csrf())
    assert r.status_code == 201, r.text
    assert c.get("/api/notes").json() == []
    assert c.post("/api/notes", body={"text": "buy milk"}, **csrf()).status_code == 201
    assert c.post("/api/notes", body={"text": ""}, **csrf()).status_code == 400
    notes = c.get("/api/notes").json()
    assert len(notes) == 1
    nid = notes[0]["id"]
    assert c.put(f"/api/notes/{nid}", body={"done": True}, **csrf()).status_code == 200
    assert c.delete(f"/api/notes/{nid}", **csrf()).json() == {"ok": True}
    assert c.get("/api/notes").json() == []
    assert c.post("/logout", **csrf()).status_code == 200
    assert c.get("/api/notes").status_code == 401


def test_browser_form_flow():
    """Same contracts over HTML forms: CSRF field + redirect + rendered list."""
    c = TestClient(app)
    tok = _csrf(c)
    c.post("/register", body=f"email=bob@ex.co&password=s3cretpw&_csrf_token={tok}",
           content_type="application/x-www-form-urlencoded")
    tok = _csrf(c)
    r = c.post("/notes", body=f"text=from+form&_csrf_token={tok}",
               content_type="application/x-www-form-urlencoded")
    assert r.status_code == 303, r.text
    assert "from form" in c.get("/notes").text
    bad = c.post("/notes", body=f"text=&_csrf_token={_csrf(c)}",
                 content_type="application/x-www-form-urlencoded")
    assert bad.status_code == 400


def test_csrf_enforced_on_browser_routes():
    c = TestClient(app)
    assert c.post("/register", body={"email": "x@ex.co", "password": "s3cretpw"}).status_code in (401, 403)
'''

REQUIREMENTS = """ikarem>=0.3.0
uvicorn>=0.24
pytest>=8
"""

ENV_EXAMPLE = """# copy to .env (or export): IKAREM_ prefix maps to app.config
IKAREM_SESSION_SECRET=generate-with-secrets-token_hex-32
IKAREM_AUTH_SECRET=generate-another-one
IKAREM_DB_URL=sqlite:///app.db
IKAREM_DEBUG=false
"""

DOCKERFILE = """FROM python:3.11-slim
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .
ENV IKAREM_DB_URL=sqlite:///data/app.db
EXPOSE 8000
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
"""

GITIGNORE = """__pycache__/
*.pyc
.pytest_cache/
.env
*.db
"""

README_MD = """# my ikarem app
```bash
pip install -r requirements.txt
cp .env.example .env   # set IKAREM_SESSION_SECRET!
pytest -q              # full-stack flow test
uvicorn app:app        # HTML at /, JSON at /api/*, docs at /docs
```
"""

FILES = {
    "app.py": APP_PY,
    "tests/test_app.py": TEST_APP_PY,
    "tests/__init__.py": "",
    "requirements.txt": REQUIREMENTS,
    ".env.example": ENV_EXAMPLE,
    "Dockerfile": DOCKERFILE,
    ".gitignore": GITIGNORE,
    "README.md": README_MD,
}


def create_project(dest: str | Path) -> Path:
    """Write the starter tree into dest (refuses non-empty dirs)."""
    root = Path(dest)
    if root.exists() and any(root.iterdir()):
        raise FileExistsError(f"{root} is not empty")
    for rel, content in FILES.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return root
