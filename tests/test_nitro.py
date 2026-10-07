"""Inbuilt accelerator: TTL + LRU + singleflight, sync and async."""

import asyncio
import threading

import pytest

from ikarem import nitro


def test_sync_hit_miss_stats_and_clear():
    calls = []

    @nitro(ttl=60.0, maxsize=8)
    def add(a, b=0):
        calls.append(1)
        return a + b

    assert add.__name__ == "add"
    assert (add(1, b=2), add(1, b=2), add(2)) == (3, 3, 2)
    info = add.cache_info()
    assert (info["hits"], info["misses"], info["coalesced"]) == (1, 2, 0)
    assert info["size"] == 2
    add.cache_clear()
    assert add.cache_info()["size"] == 0
    assert add(1, b=2) == 3 and len(calls) == 3


def test_sync_ttl_expiry_with_fake_clock():
    now = [100.0]
    calls = []

    @nitro(ttl=10.0, clock=lambda: now[0])
    def f(x):
        calls.append(1)
        return x

    assert (f(1), f(1)) == (1, 1) and len(calls) == 1
    now[0] += 9.9
    assert f(1) == 1 and len(calls) == 1  # still fresh
    now[0] += 0.2
    assert f(1) == 1 and len(calls) == 2  # expired -> recompute


def test_sync_lru_eviction():
    calls = []

    @nitro(ttl=60.0, maxsize=2)
    def f(x):
        calls.append(x)
        return x

    f(1)
    f(2)
    f(3)  # evicts 1
    assert f(1) == 1 and calls.count(1) == 2
    assert f(2) == 2 and calls.count(2) == 2  # 2 was evicted by re-adding 1


def test_sync_unhashable_args_and_kwargs_order():
    calls = []

    @nitro(ttl=60.0)
    def f(items, tag="x"):
        calls.append(1)
        return (len(items), tag)

    assert f([1, 2], tag="a") == (2, "a")
    assert f([1, 2], tag="a") == (2, "a") and len(calls) == 1
    assert f({"k": 1}) == (1, "x") and len(calls) == 2


def test_sync_exceptions_never_cached():
    calls = []

    @nitro(ttl=60.0)
    def f(fail):
        calls.append(1)
        if fail:
            raise RuntimeError("boom")
        return "ok"

    with pytest.raises(RuntimeError):
        f(True)
    with pytest.raises(RuntimeError):
        f(True)
    assert f(False) == "ok" and len(calls) == 3


def test_sync_singleflight_coalesces_threads():
    import time as _t

    calls = []

    @nitro(ttl=60.0)
    def slow(x):
        calls.append(1)
        _t.sleep(0.2)  # wide window: every thread must arrive while computing
        return x * 2

    outs, barrier = [], threading.Barrier(11)

    def worker():
        barrier.wait()
        outs.append(slow(21))

    ts = [threading.Thread(target=worker) for _ in range(10)]
    for t in ts:
        t.start()
    barrier.wait()  # release only when all 10 workers are parked at the gate
    for t in ts:
        t.join(timeout=10)
    assert outs == [42] * 10 and len(calls) == 1
    info = slow.cache_info()
    assert info["misses"] == 1 and info["coalesced"] == 9


def test_async_hit_and_stats():
    calls = []

    @nitro(ttl=60.0)
    async def f(x):
        calls.append(1)
        return x + 1

    async def go():
        assert await f(1) == 2
        assert await f(1) == 2
        assert await f(2) == 3

    asyncio.run(go())
    assert (f.cache_info()["hits"], f.cache_info()["misses"]) == (1, 2)
    assert len(calls) == 2


def test_async_singleflight_coalesces_gather():
    calls = []

    @nitro(ttl=60.0)
    async def slow(x):
        calls.append(1)
        await asyncio.sleep(0.05)
        return x

    async def go():
        return await asyncio.gather(*[slow(7) for _ in range(20)])

    assert asyncio.run(go()) == [7] * 20 and len(calls) == 1
    assert slow.cache_info()["coalesced"] == 19


def test_async_exceptions_never_cached():
    calls = []

    @nitro(ttl=60.0)
    async def f(fail=False):
        calls.append(1)
        if fail:
            raise RuntimeError("boom")
        return "ok"

    async def go():
        with pytest.raises(RuntimeError):
            await f(True)
        return await f(False)

    assert asyncio.run(go()) == "ok" and len(calls) == 2
    assert asyncio.run(f(False)) == "ok" and len(calls) == 2  # cached value is loop-free data


def test_async_stale_loop_pending_never_awaited():
    # Owner dies with its loop mid-flight: the next loop must recompute,
    # never await the dead loop's future (that would hang forever).
    # threading.Event gate: no asyncio primitive is shared across loops.
    calls = []
    gate = threading.Event()

    @nitro(ttl=60.0)
    async def g(x):
        calls.append(1)
        while not gate.is_set():
            await asyncio.sleep(0.01)
        return x

    loop1 = asyncio.new_event_loop()
    loop1.create_task(g("x"))

    async def pump():
        await asyncio.sleep(0.2)  # task registers pending, then parks in the gate

    loop1.run_until_complete(pump())
    loop1.close()  # owner destroyed mid-flight; pending now points at a dead loop
    gate.set()  # recomputes pass through instantly
    assert asyncio.run(g("x")) == "x" and len(calls) == 2


def test_bad_config_rejected_with_fix():
    with pytest.raises(ValueError, match=r"ttl>0"):
        nitro(ttl=0)
    with pytest.raises(ValueError, match=r"maxsize>=1"):
        nitro(maxsize=0)
