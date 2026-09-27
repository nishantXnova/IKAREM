"""Sustained load proof for Ledger on real uvicorn. Stdlib only.

Boots the app in-process, then hammers it with threads over KEEP-ALIVE
connections (like real clients — one connection per thread, not per request):
  - GET /healthz ceiling (public, no auth)
  - GET /api/summary (authed JSON + SQLite reads)
  - POST /api/txns (authed writes + validation + CSRF)
  - one slow-client trickle POST (raw socket, 1 byte / 50ms)

Client IPs rotate per request (the limiter is per-IP; this measures the
server, not the 240/min policy — policy itself is unit-tested).
Pass = zero 5xx, zero timeouts/hangs. 429s are counted separately.

Usage: LOAD_SECS=45 LOAD_WORKERS=16 python bench/load.py
"""

import http.client
import http.cookiejar
import json
import os
import random
import socket
import statistics
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

SECS = int(os.environ.get("LOAD_SECS", "45"))
WORKERS = int(os.environ.get("LOAD_WORKERS", "16"))
PORT = 8127

TMP = tempfile.mkdtemp(prefix="ledger-load-")
os.environ.setdefault("IKAREM_DB_URL", f"sqlite:///{TMP}/load.db")
os.environ.setdefault("IKAREM_DEMO", "false")
os.environ.setdefault("IKAREM_SESSION_SECRET", "load-test-secret")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

BASE = f"http://127.0.0.1:{PORT}"


def login(n):
    jar = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    ip = {"X-Forwarded-For": f"10.7.0.{n}"}

    def call(method, path, data=None, headers=None, timeout=15):
        body = json.dumps(data).encode() if isinstance(data, dict) else data
        h = dict(ip)
        h.update(headers or {})
        req = urllib.request.Request(BASE + path, data=body, method=method, headers=h)
        try:
            with op.open(req, timeout=timeout) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    s, b = call("GET", "/api/csrf")
    assert s == 200, (s, b[:100])
    tok = json.loads(b)["csrf"]
    email = f"load{n}-{threading.get_ident()}@{time.time_ns()}.ex"
    s, b = call(
        "POST",
        "/register",
        {"email": email, "password": "s3cretpw"},
        {"Content-Type": "application/json", "X-Csrf-Token": tok},
    )
    assert s == 201, (s, b[:200])
    cookie = "; ".join(f"{c.name}={c.value}" for c in jar)
    conn = http.client.HTTPConnection("127.0.0.1", PORT, timeout=15)
    return conn, cookie, tok


_local = threading.local()


def sess():
    """One session per worker thread — http.client conns are not shareable."""
    s = getattr(_local, "sess", None)
    if s is None:
        s = _local.sess = login(random.randrange(1_000_000))
    return s


def _health_conn():
    c = getattr(_local, "hconn", None)
    if c is None:
        c = _local.hconn = http.client.HTTPConnection("127.0.0.1", PORT, timeout=15)
    return c


def call_keepalive(conn, method, path, body=None, headers=None):
    """One persistent connection per thread; reconnect once on failure."""
    payload = json.dumps(body).encode() if isinstance(body, dict) else body
    h = {"X-Forwarded-For": f"10.8.{random.randrange(250)}.{random.randrange(250)}"}
    h.update(headers or {})
    for attempt in (0, 1):
        try:
            conn.request(method, path, body=payload, headers=h)
            r = conn.getresponse()
            data = r.read()
            return r.status, data
        except (http.client.HTTPException, OSError):
            conn.close()
            conn.connect()
    return "ERR:reconnect", b""


def hammer(seconds, fn):
    lat, codes, stop = [], {}, time.time() + seconds

    def one():
        t0 = time.perf_counter()
        try:
            s, _ = fn()
            lat.append((time.perf_counter() - t0) * 1000)
            codes[s] = codes.get(s, 0) + 1
        except Exception as e:  # noqa: BLE001 - timeouts/hangs land here
            lat.append((time.perf_counter() - t0) * 1000)
            codes[f"ERR:{type(e).__name__}"] = codes.get(f"ERR:{type(e).__name__}", 0) + 1

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = []
        while time.time() < stop:
            futs.append(ex.submit(one))
            if len(futs) > WORKERS * 4:
                futs[0].result()
                futs = futs[1:]
        for f in futs:
            f.result()
    return lat, codes


def pct(lat, p):
    return statistics.quantiles(sorted(lat), n=100)[min(p, 99) - 1] if lat else 0


def slow_trickle(cookie_header, csrf):
    """1 byte / 50ms POST body over a raw socket. Must not hang the server."""
    body = json.dumps({"description": "trickle", "amount": 1, "kind": "expense"}).encode()
    head = (
        f"POST /api/txns HTTP/1.1\r\nHost: 127.0.0.1:{PORT}\r\n"
        f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n"
        f"Cookie: {cookie_header}\r\nX-Csrf-Token: {csrf}\r\nConnection: close\r\n\r\n"
    ).encode()
    s = socket.create_connection(("127.0.0.1", PORT), timeout=30)
    sent_bytes, t_start = 0, time.time()
    if os.environ.get("LOAD_DEBUG"):
        print("HEAD:", head[:160])
        print("cookie len:", len(cookie_header), "token len:", len(csrf), "body len:", len(body))
    try:
        s.sendall(head)
        for i in range(0, len(body), 1):
            s.sendall(body[i : i + 1])
            sent_bytes += 1
            time.sleep(0.05)
        resp = b""
        while True:
            chunk = s.recv(4096)
            if not chunk:
                break
            resp += chunk
    except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError) as e:
        print(
            f"slow-client aborted after {sent_bytes}/{len(body)} bytes, "
            f"{time.time() - t_start:.2f}s: {type(e).__name__}"
        )
        raise
    finally:
        s.close()
    status = int(resp.split(b" ")[1])
    assert status == 201, resp[:200]
    return status


def main():
    import uvicorn

    server = uvicorn.Server(uvicorn.Config("ledger.app:app", host="127.0.0.1", port=PORT, log_level="error"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(300):
        if server.started:
            break
        time.sleep(0.1)
    assert server.started
    failures = []
    try:
        _conn, cookie, tok = sess()
        s = slow_trickle(cookie, tok)
        print(f"slow-client trickle POST -> {s}")

        lat, codes = hammer(10, lambda: call_keepalive(_health_conn(), "GET", "/healthz"))
        print(
            f"healthz : {len(lat) / 10:7.0f} rps  p50={pct(lat, 50):6.1f}ms p99={pct(lat, 99):6.1f}ms {codes}"
        )
        failures += [k for k in codes if isinstance(k, str) or k >= 500]

        # authed app traffic, one session per worker thread
        def authed():
            conn, cookie, _tok = sess()
            return call_keepalive(conn, "GET", "/api/summary", headers={"Cookie": cookie})

        lat, codes = hammer(SECS, authed)
        bad = {k: v for k, v in codes.items() if k != 200 and k != 429}
        print(
            f"summary : {len(lat) / SECS:7.0f} rps  p50={pct(lat, 50):6.1f}ms p99={pct(lat, 99):6.1f}ms {codes}"
        )
        failures += list(bad)

        def writer():
            conn, cookie, tok = sess()
            return call_keepalive(
                conn,
                "POST",
                "/api/txns",
                {"description": "load", "amount": 1, "kind": "expense"},
                {"Content-Type": "application/json", "Cookie": cookie, "X-Csrf-Token": tok},
            )

        lat, codes = hammer(20, writer)
        bad = {k: v for k, v in codes.items() if k not in (200, 201, 429)}
        print(
            f"writes  : {len(lat) / 20:7.0f} rps  p50={pct(lat, 50):6.1f}ms p99={pct(lat, 99):6.1f}ms {codes}"
        )
        failures += list(bad)
    finally:
        server.should_exit = True
        t.join(timeout=10)

    if failures:
        print("FAILURES:", failures)
        sys.exit(1)
    print("LOAD OK: no 5xx, no timeouts")


if __name__ == "__main__":
    main()
