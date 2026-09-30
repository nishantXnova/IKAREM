"""Bench smoke: runs without the rival installed (CI), clearly labeled."""

import asyncio
import builtins
import sys


def test_bench_degrades_without_meraki(capsys):
    import bench.bench_switch as bench

    saved = sys.modules.pop("meraki", None)
    real_import = builtins.__import__

    def _blocked(name, *a, **k):
        if name == "meraki" or name.startswith("meraki."):
            raise ModuleNotFoundError("No module named 'meraki'")
        return real_import(name, *a, **k)

    old_n = bench.N
    bench.N = 20
    builtins.__import__ = _blocked
    try:
        asyncio.run(bench.main())
    finally:
        builtins.__import__ = real_import
        bench.N = old_n
        if saved is not None:
            sys.modules["meraki"] = saved
    out = capsys.readouterr().out
    assert "meraki not installed" in out
    assert "ikarem" in out


def test_bench_hit_correctness():
    import bench.bench_switch as bench

    async def _go():
        app = bench.build_ikarem_native()
        status, _ = await bench.hit(app, "GET", "/hello")
        assert status == 200
        status, _ = await bench.hit(app, "GET", "/missing")
        assert status == 404

    asyncio.run(_go())
