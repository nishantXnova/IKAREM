"""Nasty SpikeManager proof on real uvicorn. Stdlib only.

Verdict contract (stated before running):
  GOOD = zero 500s, zero timeouts/hangs, 100% exempt-probe success
         during the spike, clean recovery after.
  BAD  = anything else -> tune further.

Phases:
  1. baseline   — light /fast traffic, expect all 200
  2. NASTY       — 64 threads vs /slow (0.3s handler) + /fast mix;
                   shedding (503 + Retry-After) is SUCCESS, not failure
  3. probes     — /healthz hammered from a separate thread DURING phase 2
  4. recovery   — traffic stops, /fast must return to all-200 and the
                   snapshot must show a drained gate

Usage: SPIKE_SECS=15 python bench/spike_nasty.py
"""

import http.client
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

SECS = int(os.environ.get("SPIKE_SECS", "15"))
PORT = 8128

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from ikarem import Ikarem  # noqa: E402
from ikarem.resilience import SpikeManager  # noqa: E402

mgr = SpikeManager(
    initial=20,
    min_limit=5,
    max_limit=100,
    target_latency=0.05,
    queue_timeout=0.2,
    retry_after=1,
    exempt_paths=("/healthz",),
)
app = Ikarem(enable_docs=False)
app.use(mgr)


@app.get("/fast")
async def fast(req):
    return {"ok": True}


@app.get("/slow")
async def slow(req):
    import asyncio as _aio

    await _aio.sleep(0.3)
    return {"ok": True}


@app.get("/debug/spike")
async def spike_debug(req):
    return mgr.snapshot()


def call(conn, method, path, timeout=20):
    try:
        conn.request(method, path)
        r = conn.getresponse()
        body = r.read()
        retry = dict(r.getheaders()).get("retry-after")
        return r.status, body, retry
    except Exception as e:  # noqa: BLE001 - timeouts/hangs land here
        return f"ERR:{type(e).__name__}", b"", None


_local = threading.local()


def conn():
    c = getattr(_local, "conn", None)
    if c is None:
        c = _local.conn = http.client.HTTPConnection("127.0.0.1", PORT, timeout=20)
    return c


def hammer(seconds, fn, workers):
    codes, lats = {}, []
    stop = time.time() + seconds

    def one():
        t0 = time.perf_counter()
        s, _, _ = fn()
        lats.append((time.perf_counter() - t0) * 1000)
        codes[s] = codes.get(s, 0) + 1

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = []
        while time.time() < stop:
            futs.append(ex.submit(one))
            if len(futs) > workers * 4:
                futs[0].result()
                futs = futs[1:]
        for f in futs:
            f.result()
    return codes, lats


def main():
    import uvicorn

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="error"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(300):
        if server.started:
            break
        time.sleep(0.1)
    assert server.started
    failures = []
    try:
        codes, _ = hammer(3, lambda: call(conn(), "GET", "/fast"), 10)
        print(f"baseline : {codes}")
        failures += [k for k in codes if k != 200]

        probe_codes, stop_flag = {}, {"stop": False}

        def probe_loop():
            c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=20)
            while not stop_flag["stop"]:
                s, _, _ = call(c, "GET", "/healthz")
                probe_codes[s] = probe_codes.get(s, 0) + 1

        pt = threading.Thread(target=probe_loop, daemon=True)
        pt.start()
        snap_before = mgr.snapshot()["limit"]

        def nasty():
            import random

            path = "/slow" if random.random() < 0.7 else "/fast"
            return call(conn(), "GET", path)

        # track Retry-After on sheds during the spike itself
        shed_with_retry = {"n": 0}

        def nasty_tracked():
            s, _, retry = nasty()
            if s == 503 and retry:
                shed_with_retry["n"] += 1
            return s, b"", retry

        codes, lats = hammer(SECS, nasty_tracked, 64)
        stop_flag["stop"] = True
        pt.join(timeout=10)
        lats.sort()
        p99 = lats[int(len(lats) * 0.99)] if lats else 0
        print(f"NASTY    : {codes} p99={p99:.0f}ms limit {snap_before}->{mgr.snapshot()['limit']}")
        print(f"probes   : {probe_codes} (during spike)")
        print(f"shed headers: {shed_with_retry['n']}/{codes.get(503, 0)} sheds carried Retry-After")
        bad = {k: v for k, v in codes.items() if k not in (200, 503)}
        failures += list(bad)
        failures += [k for k in probe_codes if k != 200]
        if codes.get(503, 0) and not shed_with_retry["n"]:
            failures.append("shed-without-retry-after")

        time.sleep(2)  # let the gate drain
        snap = mgr.snapshot()
        print(f"snapshot : {snap}")
        codes, _ = hammer(3, lambda: call(conn(), "GET", "/fast"), 10)
        snap_after = mgr.snapshot()
        print(f"recovery : {codes} limit {snap['limit']}->{snap_after['limit']}")
        failures += [k for k in codes if k != 200]
        if snap_after["limit"] <= snap["limit"]:
            failures.append(f"limit-never-reopened:{snap_after['limit']}")
        if snap["in_flight"] != 0:
            failures.append(f"gate-not-drained:{snap['in_flight']}")
    finally:
        server.should_exit = True
        t.join(timeout=10)

    if failures:
        print("VERDICT: BAD —", failures)
        sys.exit(1)
    print("VERDICT: GOOD — no 500s, no hangs, probes 100%, recovery clean")


if __name__ == "__main__":
    main()
