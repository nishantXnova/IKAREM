# NISH responses: APIs the Viewer extension paints

The NISH Viewer browser extension (the NISH project's `web/` folder —
sideload it unpacked via `chrome://extensions`) watches page bodies: when
the first non-empty line is `NISH/1.0`, it parses the document and renders
a formatted tree. `ikarem/nish.py` exists so your endpoints can feed it —
a stdlib-only writer for the response subset, no new dependency.

## One line per route

```python
from ikarem import Ikarem, negotiate

app = Ikarem()


@app.get("/api/summary")
async def summary(req):
    data = {"income_cents": 200000, "rows": [{"id": 1}]}
    return negotiate(req, data)
```

- Default: JSON, unchanged. Every existing client keeps working.
- `?format=nish`: NISH text. Open it in a browser with the extension and
  the tree paints itself; without the extension it reads as clean text.
- `Accept: application/x-nish` (or anything mentioning `nish`): same NISH
  answer for machine clients that ask for it.

Live proof: Ledger's `/api/summary` negotiates — log in at `/login`
(`demo@example.com` / `demo1234`), then open
`/api/summary?format=nish` with the extension installed.

## Full duplex: reading NISH back

Clients can POST NISH as well as GET it. `from_nish()` parses core
documents (sections, arrays, dotted keys, comments, ext tags as
`{"$tag", "value"}`, anchors) and `await req.nish()` reads a request
body — blank yields `None` like `json()`, malformed raises 400 naming
the line:

```python
@app.post("/notes")
async def create(req):
    body = await req.nish()
    return {"got": body["text"]}
```

The reader agrees with the reference engine on 29 edge cases (bare
words, `inf`/`nan` kept as strings, duplicate keys rejected, lenient
base64, unparseable timestamps preserved) with two documented
differences: a missing `NISH/x.y` header is accepted, and ext values
arrive as plain dicts (JSON-serializable) instead of `Ext` objects.

## Free 304s: content-hash ETags

Every `NISHResponse` carries `ETag: "<sha256-of-bytes>"`. Add one line
and repolls stop costing bodies:

```python
app.use(ConditionalMiddleware())
```

`GET /api/summary?format=nish` twice with `If-None-Match` returns 304
with an empty body the second time. The middleware is generic — any
response carrying an ETag qualifies, NISH or otherwise. Non-GET and
ETag-less responses pass through untouched.

## Config files and self-describing APIs

`Config.load_nish("app.nish")` loads typed config at dict/file
precedence (below env, no string coercion — the format already typed
it). And every app serves its own contract twice: `/openapi.json` plus
`/openapi.nish`, both generated from the same compiled plans.

## Why text/plain (read this before "fixing" it)

Browsers *download* unknown MIME types instead of rendering them, so
`application/x-nish` would never reach the extension — there would be no
page to beautify. `NISHResponse` therefore defaults to
`text/plain; charset=utf-8` (renders; the extension sniffs the `NISH/1.0`
first line) and takes `media_type="application/x-nish"` for strict
machine APIs that never open in a browser.

## What the writer covers

Scalars (`null`, bools, ints, floats with the load-bearing `N.0`
integral rule, strings with escapes, `bytes:b64:`, `time:` RFC3339),
lists (inline, or `[[table]]` blocks for arrays of maps at the top
level), and nested maps (`[section]` / `[a.b]`). Bare keys stay bare;
anything that would compose (`dotted.key`) or confuse is quoted.

Outside the subset it raises instead of degrading: non-dict roots,
non-string keys, unknown value types (`TypeError`), non-finite floats
(`ValueError`). No anchors/graphs, no ext tags, no binary twin — the
full format lives in the `nish-format` engines.

## Proven compatible, not claimed

The writer's output round-trips through both real engines: the Python
`nish.loads` (scalars, int/float distinction, strings, nesting, arrays,
keys, empty, bytes, datetime, all four error cases) and the JS
`Nish.parse` (the same parser the extension ships). The committed suite
(`tests/test_nish.py`) pins exact output strings so compatibility can't
drift without a red build.
