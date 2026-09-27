"""`ikarem run|check|mcp|new ...`."""

from __future__ import annotations

import argparse
import asyncio
import importlib
import sys


def _load(target: str):
    mod, _, attr = target.partition(":")
    return getattr(importlib.import_module(mod), attr or "app")


def _cmd_run(app, host: str, port: int, reload: bool) -> None:
    app.run(host=host, port=port, reload=reload)


def _cmd_check(app) -> int:
    report = (
        app.check()
        if hasattr(app, "check")
        else __import__("ikarem.compiled", fromlist=["check_app"]).check_app(app)
    )
    routes = report.get("routes", [])
    warnings = report.get("warnings", [])
    errors = report.get("errors", [])
    print(f"checked {len(routes)} route(s)")
    for r in routes:
        print(f"  {'/'.join(sorted(r['methods']))} {r['path']} -> {r['handler']}")
    for w in warnings:
        print(f"WARN: {w}")
    for e in errors:
        print(f"ERROR: {e}")
    if errors:
        print(f"check FAILED: {len(errors)} error(s)")
        return 1
    print("check OK" + (f" ({len(warnings)} warning(s))" if warnings else ""))
    return 0


def main() -> None:
    # Backward compat: bare `ikarem module:app [--host/--port/--reload]` means run.
    if len(sys.argv) > 1 and sys.argv[1] not in ("run", "check", "mcp", "new", "-h", "--help"):
        p = argparse.ArgumentParser(prog="ikarem", description="IKAREM runner")
        p.add_argument("target", nargs="?", default="examples.basic:app")
        p.add_argument("--host", default="127.0.0.1")
        p.add_argument("--port", type=int, default=8000)
        p.add_argument("--reload", action="store_true")
        args = p.parse_args()
        _cmd_run(_load(args.target), args.host, args.port, args.reload)
        return

    p = argparse.ArgumentParser(prog="ikarem", description="IKAREM runner")
    sub = p.add_subparsers(dest="cmd", required=True)

    pr = sub.add_parser("run", help="serve the app")
    pr.add_argument("target", nargs="?", default="examples.basic:app", help="module:attr, e.g. myapp:app")
    pr.add_argument("--host", default="127.0.0.1")
    pr.add_argument("--port", type=int, default=8000)
    pr.add_argument("--reload", action="store_true")

    pc = sub.add_parser("check", help="statically compile + audit all handlers")
    pc.add_argument("target", nargs="?", default="examples.basic:app", help="module:attr, e.g. myapp:app")

    pm = sub.add_parser("mcp", help="serve routes as MCP tools over stdio")
    pm.add_argument("target", nargs="?", default="examples.basic:app", help="module:attr, e.g. myapp:app")

    pn = sub.add_parser("new", help="scaffold a production-grade starter project")
    pn.add_argument("dir", help="directory to create (must not exist or be empty)")

    args = p.parse_args()
    if args.cmd == "new":
        from .scaffold import create_project

        try:
            root = create_project(args.dir)
        except FileExistsError as e:
            print(f"error: {e}")
            raise SystemExit(1)
        print(f"created {root}")
        print("next: pip install -r requirements.txt; pytest -q; uvicorn app:app")
        print("set IKAREM_SESSION_SECRET before deploying!")
        raise SystemExit(0)
    app = _load(args.target)
    if args.cmd == "run":
        _cmd_run(app, args.host, args.port, args.reload)
    elif args.cmd == "mcp":
        raise SystemExit(asyncio.run(app.mcp_server().run_stdio()))
    else:
        raise SystemExit(_cmd_check(app))


if __name__ == "__main__":
    main()
