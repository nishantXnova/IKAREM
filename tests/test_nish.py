"""NISH writer + reader: exact output, engine-agreed parsing, negotiation."""

import datetime

import pytest

from ikarem import ConditionalMiddleware, Ikarem
from ikarem.nish import NISHResponse, from_nish, negotiate, to_nish
from ikarem.testing import TestClient


def test_writer_scalars_and_viewer_first_line():
    out = to_nish({"ok": True, "n": 7, "pi": 3.14, "big": 1000.0, "nothing": None})
    assert out.splitlines()[0] == "NISH/1.0"  # the Viewer extension sniffs this
    assert "ok = true" in out
    assert "n = 7" in out
    assert "pi = 3.14" in out
    assert "big = 1000.0" in out  # integral floats keep .0 (int/float loads differ)
    assert "nothing = null" in out


def test_writer_string_escapes():
    out = to_nish({"q": 'a"b\\c\nd\teé\x01', "empty": ""})
    assert '"a\\"b\\\\c\\nd\\teé\\u0001"' in out
    assert 'empty = ""' in out


def test_writer_sections_and_arrays():
    out = to_nish(
        {
            "title": "shop",
            "user": {"name": "amy", "tags": ["a", "b"]},
            "rows": [{"id": 1}, {"id": 2}],
            "mixed": [1, "two", None],
        }
    )
    # plain keys first: nothing binds inside a section by accident
    assert out.index("mixed = ") < out.index("[[rows]]")
    assert "[user]" in out
    assert "[[rows]]" in out
    assert out.count("id = 1") == 1 and out.count("id = 2") == 1


def test_writer_quotes_composing_keys():
    # bare dotted keys would compose (a.b nests); quoting keeps them literal
    out = to_nish({"weird key": 1, "dotted.key": 2, "123abc": 3})
    assert '"weird key" = 1' in out
    assert '"dotted.key" = 2' in out
    assert '"123abc" = 3' in out


def test_writer_bytes_and_dates():
    import base64

    out = to_nish({"blob": b"\x00hi", "day": datetime.date(2026, 1, 2)})
    assert "bytes:b64:" + base64.b64encode(b"\x00hi").decode() in out
    assert "time:2026-01-02T00:00:00Z" in out


def test_writer_rejects_outside_subset():
    with pytest.raises(TypeError, match="wrap it in a dict"):
        to_nish([1])
    with pytest.raises(TypeError, match="no encoding"):
        to_nish({"x": object()})
    with pytest.raises(ValueError, match="non-finite"):
        to_nish({"x": float("nan")})
    with pytest.raises(TypeError, match="must be strings"):
        to_nish({1: 2})


def test_nish_response_shape():
    r = NISHResponse({"a": 1})
    assert r.body.startswith(b"NISH/1.0")
    assert r.status_code == 200
    assert "text/plain" in r.media_type  # renders in browsers for the extension


def test_negotiate_query_and_accept_header():
    app = Ikarem(enable_docs=False)

    @app.get("/d")
    async def d(req):
        return negotiate(req, {"n": 1})

    c = TestClient(app)
    assert c.get("/d").json() == {"n": 1}  # default stays JSON
    nish = c.get("/d", query="format=nish")
    assert nish.body.startswith(b"NISH/1.0") and b"n = 1" in nish.body
    acc = c.get("/d", headers={"accept": "application/x-nish"})
    assert acc.body.startswith(b"NISH/1.0")


def test_reader_sections_arrays_dotted_keys():
    doc = (
        'NISH/1.0\ntitle = "shop"\n[user]\nname = "amy"\n'
        '[user.profile]\nbio = "hi"\n[[orders]]\nid = 1\n[[orders]]\nid = 2\n'
    )
    assert from_nish(doc) == {
        "title": "shop",
        "user": {"name": "amy", "profile": {"bio": "hi"}},
        "orders": [{"id": 1}, {"id": 2}],
    }


def test_reader_comments_anchors_ext():
    doc = 'NISH/1.0\n# hello\na = 1 # trailing\nowner = &me {name = "n"}\nreviewer = *me\nc = !nish.color "#ff00aa"\n'
    out = from_nish(doc)
    assert out["a"] == 1 and out["owner"] == {"name": "n"} and out["reviewer"] == {"name": "n"}
    assert out["c"] == {"$tag": "nish.color", "value": "#ff00aa"}


def test_reader_types_and_errors():
    assert from_nish("d = bytes:b64:SGVsbG8=") == {"d": b"Hello"}
    assert from_nish("t = time:2026-09-30T12:00:00Z") == {
        "t": datetime.datetime(2026, 9, 30, 12, 0, tzinfo=datetime.timezone.utc)
    }
    assert from_nish("t = time:nope") == {"t": "nope"}  # unparseable payload kept, like the engine
    assert from_nish("x = hello") == {"x": "hello"}  # bare words are strings, like the engine
    with pytest.raises(ValueError, match="line 3"):
        from_nish("NISH/1.0\na = [1,\n")
    with pytest.raises(ValueError, match="duplicate key"):
        from_nish("a = 1\na = 2\n")
    with pytest.raises(ValueError, match="redefines"):
        from_nish("a = 1\n[a]\n")


def test_request_nish_round_trip_and_400s():
    app = Ikarem(enable_docs=False)

    @app.post("/echo")
    async def echo(req):
        body = await req.nish()
        return {"got": body["n"]}

    @app.post("/maybe")
    async def maybe(req):
        return {"blank": (await req.nish()) is None}

    c = TestClient(app)
    assert c.post("/echo", body="NISH/1.0\nn = 3\n", content_type="text/plain").json() == {"got": 3}
    assert c.post("/echo", body="n = [\n", content_type="text/plain").status_code == 400
    assert c.post("/maybe", body="").json() == {"blank": True}


def test_etag_and_conditional_304():
    app = Ikarem(enable_docs=False)
    app.use(ConditionalMiddleware())

    @app.get("/d")
    async def d(req):
        return NISHResponse({"n": 1})

    @app.get("/plain")
    async def plain(req):
        from ikarem import TextResponse

        return TextResponse("hi")  # no ETag: middleware invisible

    c = TestClient(app)
    first = c.get("/d")
    etag = first.headers.get("etag")
    assert etag and etag.startswith('"')
    again = c.get("/d", headers={"if-none-match": etag})
    assert again.status_code == 304 and again.body == b""
    assert c.get("/d", headers={"if-none-match": '"other"'}).status_code == 200
    assert c.get("/plain", headers={"if-none-match": "*"}).status_code == 200
    assert c.get("/d", headers={"if-none-match": "*"}).status_code == 304


def test_nish_mode_switch():
    from ikarem import Schema

    app = Ikarem(enable_docs=False)
    assert app.nish is False

    class Item(Schema):
        name: str

    @app.get("/d")
    async def d(req):
        return {"n": 1}

    @app.get("/lst")
    async def lst(req):
        return [1, 2]

    @app.post("/items")
    async def create(item: Item):
        return {"name": item.name}

    app.nish = True  # the property spelling; method spelling below
    assert app.nish is True

    c = TestClient(app)
    plain = c.get("/d")
    assert plain.json() == {"n": 1}  # default untouched
    assert "etag" in plain.headers  # ETags automatic, both shapes
    assert c.get("/d", headers={"if-none-match": plain.headers["etag"]}).status_code == 304
    nish = c.get("/d", query="format=nish")
    assert nish.body.startswith(b"NISH/1.0") and b"n = 1" in nish.body
    assert "etag" in nish.headers
    assert nish.headers["etag"] != plain.headers["etag"]  # different bytes, different validator
    assert (
        c.get("/d", query="format=nish", headers={"if-none-match": nish.headers["etag"]}).status_code == 304
    )
    assert c.get("/d", headers={"accept": "application/x-nish"}).body.startswith(b"NISH/1.0")
    stayed = c.get("/lst", query="format=nish")
    assert stayed.json() == [1, 2]  # NISH documents are maps; lists stay JSON
    missing = c.get("/nope", query="format=nish")
    assert missing.status_code == 404 and missing.body.startswith(b"NISH/1.0")
    assert b"detail" in missing.body  # errors in NISH too
    bad = c.post("/items", body={}, query="format=nish")
    assert bad.status_code == 400 and bad.body.startswith(b"NISH/1.0")


def test_nish_mode_method_config_idempotent(tmp_path):
    from ikarem.conditional import ConditionalMiddleware
    from ikarem.nish import _NISHNegotiation

    (tmp_path / "app.nish").write_text("NISH/1.0\n\npage_size = 42\n")
    app = Ikarem(enable_docs=False)
    out = app.nish_mode(config=str(tmp_path / "app.nish"))
    assert out is app and app.config.get("page_size") == 42
    app.nish_mode()  # second call: no duplicate layers
    kinds = [type(m) for m in app.middleware.stack]
    assert kinds.count(ConditionalMiddleware) == 1
    assert kinds.count(_NISHNegotiation) == 1
    with pytest.raises(RuntimeError, match="one-way"):
        app.nish = False


def test_nish_mode_string_property_loads_config(tmp_path):
    (tmp_path / "a.nish").write_text("NISH/1.0\nflag = true\n")
    app = Ikarem(enable_docs=False)
    app.nish = str(tmp_path / "a.nish")
    assert app.nish is True and app.config.get("flag") is True


def test_config_load_nish(tmp_path):
    from ikarem import Config

    (tmp_path / "app.nish").write_text("NISH/1.0\n\npage_size = 25\ndebug = false\n")
    cfg = Config()
    cfg.load_nish(str(tmp_path / "app.nish"))
    assert cfg.get("page_size") == 25 and cfg.get("debug") is False


def test_openapi_nish_self_hosting():
    app = Ikarem()

    @app.get("/items/{uid:int}")
    async def get_user(req, uid: int):
        return {"uid": uid}

    c = TestClient(app)
    r = c.get("/openapi.nish")
    assert r.status_code == 200 and r.body.startswith(b"NISH/1.0")
    spec = from_nish(r.text)
    assert "/users/{uid}" not in spec["paths"]
    assert "get" in spec["paths"]["/items/{uid}"]
