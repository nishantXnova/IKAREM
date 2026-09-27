"""Forms + file uploads: urlencoded, multipart, limits."""

from ikarem import Ikarem, PayloadTooLarge
from ikarem.http import UploadFile
from ikarem.testing import TestClient


def _app():
    app = Ikarem(enable_docs=False)

    @app.post("/url")
    async def url(req):
        form = await req.form()
        return {"name": form.get("name"), "tags": form.getlist("tags")}

    @app.post("/up")
    async def up(req):
        form = await req.form(max_file_size=100)
        f = form.get("doc")
        assert isinstance(f, UploadFile)
        return {"filename": f.filename, "size": f.size, "title": form.get("title"), "ct": f.content_type}

    @app.post("/small")
    async def small(req):
        await req.form(max_form_size=10)
        return {"ok": True}

    return app


def test_urlencoded_and_repeated_fields():
    c = TestClient(_app())
    r = c.post("/url", body="name=amy&tags=a&tags=b", content_type="application/x-www-form-urlencoded")
    assert r.status_code == 200, r.text
    assert r.json() == {"name": "amy", "tags": ["a", "b"]}


def _multipart(fields, files, boundary="BOUNDARY123"):
    parts = []
    for k, v in fields:
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n')
    for k, (fn, ct, data) in files:
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"; filename="{fn}"'
            f"\r\nContent-Type: {ct}\r\n\r\n{data}\r\n"
        )
    parts.append(f"--{boundary}--\r\n")
    return "".join(parts).encode(), f"multipart/form-data; boundary={boundary}"


def test_multipart_with_file():
    c = TestClient(_app())
    body, ct = _multipart([("title", "hello")], [("doc", ("a.txt", "text/plain", "FILEDATA"))])
    r = c.post("/up", body=body, content_type=ct)
    assert r.status_code == 200, r.text
    assert r.json() == {"filename": "a.txt", "size": 8, "title": "hello", "ct": "text/plain"}


def test_form_size_limit_is_413():
    c = TestClient(_app())
    r = c.post("/small", body="x" * 100, content_type="application/x-www-form-urlencoded")
    assert r.status_code == 413


def test_file_size_limit_is_413():
    c = TestClient(_app())
    body, ct = _multipart([], [("doc", ("big.bin", "application/octet-stream", "z" * 200))])
    assert c.post("/up", body=body, content_type=ct).status_code == 413


def test_missing_boundary_is_413():
    c = TestClient(_app())
    assert c.post("/up", body=b"junk", content_type="multipart/form-data").status_code == 413


def test_payload_too_large_exported():
    assert PayloadTooLarge.status_code == 413


def test_schema_param_accepts_form_bodies():
    from ikarem import Field, Schema

    app = Ikarem(enable_docs=False)

    class Note(Schema):
        text: str = Field(..., min_length=1, max_length=100)

    @app.post("/n")
    async def h(req, note: Note):
        return {"text": note.text}

    c = TestClient(app)
    assert c.post("/n", body="text=hi", content_type="application/x-www-form-urlencoded").json() == {
        "text": "hi"
    }
    assert c.post("/n", body={"text": "yo"}).json() == {"text": "yo"}
    assert c.post("/n", body="text=", content_type="application/x-www-form-urlencoded").status_code == 400
