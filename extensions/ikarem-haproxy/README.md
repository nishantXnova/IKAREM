# ikarem-haproxy — an IKAREM-aware HAProxy edge

HAProxy is the reference reverse proxy of the ecosystem: TLS termination,
health-checked backends, per-IP stick tables, reloads that keep sockets
alive. It knows nothing about IKAREM — it cannot see your blueprints,
your WebSocket routes or where your auth boundary is.

This extension teaches it. One command reads the **compiled plans that
already serve your traffic** and emits a complete `haproxy.cfg` whose
backends, ACLs and health checks match the app it fronts.

Stdlib only. The core stays zero-dep; HAProxy itself is an external
binary, not a Python dependency.

## Quickstart

```bash
pip install -e ./extensions/ikarem-haproxy
```

```bash
# 1. look at what the edge will do
ikarem-haproxy zones myapp:app

# 2. generate, then let HAProxy validate the file
ikarem-haproxy config myapp:app -o haproxy.cfg --verify

# 3. run it in the foreground (Ctrl-C stops it)
ikarem-haproxy run myapp:app -- --log-stderr
```

The app still needs to be running; HAProxy fronts it:

```bash
ikarem run myapp:app --port 8000
```

### Options that matter

| Flag | Default | Effect |
|---|---|---|
| `--listen` | `0.0.0.0:8080` | plain-HTTP bind (front door) |
| `--upstream` | `127.0.0.1:8000` | IKAREM address; repeat or comma-separate to `roundrobin` several workers |
| `--rate-limit N` | `0` (off) | per-IP cap per 10s at the edge, same 429 body/`Retry-After` as IKAREM |
| `--static-cache S` | `0` (off) | `Cache-Control: public, max-age=S, immutable` for `/static` |
| `--tls-cert PEM` | none | terminate TLS; adds a `:80` → `https` 308 redirect frontend |
| `--stats` | off | stats page on `127.0.0.1:8404` |
| `--single-backend` | off | one backend for every path (no per-blueprint routing) |
| `--health-path` | `/healthz` | probe path; a warning (and a config comment) if it is not a real route |

`IKAREM_HAPROXY=/opt/haproxy` and `--haproxy` both override binary
discovery. `config -o -` prints to stdout; `check --config file.cfg`
validates a file you already have.

## What is derived from your app (not from flags)

- **One backend per blueprint/route group.** `billing = Blueprint(...)`
  becomes `backend zone_billing` with `acl zone_billing path_beg /billing`.
  A group that owns several prefixes gets all of them on one line
  (`path_beg /debug /static /ws`); the group that owns `/` is the
  `default_backend`, so unmatched paths land where the app's own 404 lives.
- **Health checks hit IKAREM's probe** (`GET /healthz`, expect 200), not a
  bare TCP connect: a 200 means the app answers, not just that a socket is
  listening.
- **WebSocket routes are detected** from the compiled plan and the config
  sets `timeout tunnel 1h`; the default 30s client timeout would kill a
  chat upgrade mid-conversation.
- **Auth routes are counted, never intercepted.** The header line says
  `3 auth`, so you can see the boundary at a glance — and the config says
  outright that HAProxy does not inspect tokens.
- **`X-Forwarded-For` is overwritten, not appended.** This is the whole
  reason to put IKAREM behind an edge: `RateLimitMiddleware` trusts the
  first `X-Forwarded-For` entry, so an appended header lets a client pick
  its own bucket. Overwriting with `%[src]` makes the app's per-IP limiter
  trustworthy — and only while this edge is the sole ingress to the
  upstream port (the config says so in a comment).

## What it deliberately does not do

- **No JWT/OAuth validation at the edge.** IKAREM's compiled plans enforce
  `Depends(require_roles(...))` with the app's own secret; duplicating that
  in HAProxy means two sources of truth and a rotation bug waiting to
  happen. The generated file states this.
- **No request-body inspection, no WAF rules.** App-level validation stays
  in the app.
- **No adapter layer.** This is not a compatibility bridge: HAProxy forwards
  raw HTTP and the request still runs IKAREM's full native pipeline
  (`check` findings, compiled plans, middleware onion). It adds bytes in
  front, not a second execution path.
- **No config mutation at runtime.** Regenerate and `haproxy -c`, then
  reload — the config is a build artifact, not a service.

Everything else is stock HAProxy, so its documentation is your
documentation.

## Requirements and honest limits

- **HAProxy 3.0+** on PATH (`winget install haproxy`, `brew install
  haproxy`, `apt install haproxy`), or `--haproxy /path/to/haproxy` /
  `IKAREM_HAPROXY`. The 429 rule uses the `deny … string … hdr …` grammar
  introduced in 3.0 for an exact-shape body.
- `check` and `run` shell out to that binary. The test suite injects the
  subprocess boundary, so it never needs HAProxy installed — but that also
  means CI proves the *config shape*, not that a given HAProxy build
  accepts it. Run `ikarem-haproxy config myapp:app --verify` on the machine
  that will serve traffic.
- `--upstream` takes `host:port`; IPv6 must be bracketed (`[::1]:8000`).
  Unix sockets, PROXY protocol and multi-process HAProxy are not generated
  — add them by hand in a copy, and keep the copy out of version control.

## Removal path (what you keep)

Delete `haproxy.cfg`, stop HAProxy, and run `ikarem run myapp:app --port
8080` directly. Nothing in your app changes: this extension never imports
into your process, never registers a plugin and never wraps the ASGI app.

| Edge feature | Native IKAREM equivalent |
|---|---|
| `/healthz` checks | `mount_system_routes(app)` — what `ikarem run` mounts (`/healthz`, `/readyz`, `/metrics`) |
| per-IP 429s | `RateLimitMiddleware`, which is what the edge copy mirrors |
| security headers | `SecurityHeadersMiddleware` |
| TLS | terminate at your platform (Caddy, nginx, a cloud LB) and forward `X-Forwarded-Proto` |
| load balancing | several `ikarem run --port` processes + your platform's LB |

## Tests

```bash
python -m pytest extensions/ikarem-haproxy/tests -q
```

30 tests pin the promises above: zone/ACL mapping from blueprints, root
group as default backend, health-check and forwarding lines, TLS redirect +
HSTS, the 429 shape and argument order, static cache as an edge rule,
deterministic LF-only output, atomic writes, and every error message
naming its remedy.
