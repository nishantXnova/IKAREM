"""`ikarem run|check|mcp|new|migrate|worker|inspect ...`."""

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


def _cmd_check(app, args) -> int:
    report = (
        app.check()
        if hasattr(app, "check")
        else __import__("ikarem.compiled", fromlist=["check_app"]).check_app(app)
    )
    routes = report.get("routes", [])
    warnings = report.get("warnings", [])
    errors = report.get("errors", [])
    if getattr(args, "format", "text") == "json":
        import json

        print(json.dumps(report, indent=1, default=str))
    else:
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
    if warnings and getattr(args, "strict", False):
        print(f"check FAILED: {len(warnings)} warning(s) under --strict")
        return 1
    if getattr(args, "format", "text") != "json":
        print("check OK" + (f" ({len(warnings)} warning(s))" if warnings else ""))
    return 0


def main() -> None:
    # Backward compat: bare `ikarem module:app [--host/--port/--reload]` means run.
    if len(sys.argv) > 1 and sys.argv[1] not in (
        "run",
        "check",
        "mcp",
        "new",
        "migrate",
        "worker",
        "inspect",
        "-h",
        "--help",
    ):
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
    pc.add_argument("--strict", action="store_true", help="warnings fail the audit too")
    pc.add_argument("--format", choices=["text", "json"], default="text", help="machine-readable report")

    pm = sub.add_parser("mcp", help="serve routes as MCP tools over stdio")
    pm.add_argument("target", nargs="?", default="examples.basic:app", help="module:attr, e.g. myapp:app")
    pm.add_argument("--list", action="store_true", help="print tools and exit (no stdio server)")

    pn = sub.add_parser("new", help="scaffold a production-grade starter project")
    pn.add_argument("dir", help="directory to create (must not exist or be empty)")

    pmg = sub.add_parser("migrate", help="versioned schema migrations: up|down|status|new")
    pmg.add_argument("action", choices=["up", "down", "status", "new"])
    pmg.add_argument("target", nargs="?", default="examples.basic:app", help="module:attr")
    pmg.add_argument("--dir", default="migrations", help="migrations directory")
    pmg.add_argument("--to", type=int, default=None, help="migrate up to version N")
    pmg.add_argument("--steps", type=int, default=1, help="migrate down N steps")
    pmg.add_argument("--name", default="migration", help="name for `migrate new`")

    pw = sub.add_parser("worker", help="drain the app's durable task queue until interrupted")
    pw.add_argument("target", nargs="?", default="examples.basic:app", help="module:attr")
    pw.add_argument("--poll", type=float, default=1.0)
    pw.add_argument(
        "--queue", default="default", help="queue name (informational; app.state_queue is drained)"
    )

    pi = sub.add_parser("inspect", help="print a compact route manifest (built for LLM context)")
    pi.add_argument("target", nargs="?", default="examples.basic:app", help="module:attr")
    pi.add_argument("--format", choices=["json", "summary", "openapi", "auth"], default="json")

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
        if args.list:
            raise SystemExit(_cmd_mcp_list(app))
        raise SystemExit(asyncio.run(app.mcp_server().run_stdio()))
    elif args.cmd == "migrate":
        raise SystemExit(_cmd_migrate(app, args))
    elif args.cmd == "worker":
        raise SystemExit(_cmd_worker(app, args))
    elif args.cmd == "inspect":
        raise SystemExit(_cmd_inspect(app, args))
    else:
        raise SystemExit(_cmd_check(app, args))


def _cmd_mcp_list(app) -> int:
    tools = app.mcp_tools()
    print(f"{len(tools)} tool(s)")
    for t in tools:
        print(f"  {t['name']}: {t.get('description', '')[:120]}")
    return 0


def _cmd_inspect(app, args) -> int:
    import json

    from .compiled import describe_app

    if hasattr(app, "_ensure_system_routes"):
        app._ensure_system_routes()
    if hasattr(app, "compile_all"):
        app.compile_all()
    manifest = describe_app(app)
    if args.format == "summary":
        print(f"{manifest['count']} route(s)")
        for r in manifest["routes"]:
            auth = f" [auth:{r['auth']['scheme']}]" if "auth" in r else ""
            print(f"  {r['method']:6} {r['path']} -> {r['handler']}{auth}")
    elif args.format == "openapi":
        from .openapi import build_openapi

        print(json.dumps(build_openapi(app), indent=1))
    elif args.format == "auth":
        print(f"{manifest['count']} route(s)")
        for r in manifest["routes"]:
            auth = r.get("auth")
            if auth is None:
                print(f"  {r['method']:6} {r['path']} -> {r['handler']} [public]")
            else:
                detail = auth["scheme"]
                if auth.get("roles"):
                    detail += f" roles={','.join(auth['roles'])}"
                if auth.get("scopes"):
                    detail += f" scopes={','.join(auth['scopes'])}"
                print(f"  {r['method']:6} {r['path']} -> {r['handler']} [auth:{detail}]")
    else:
        print(json.dumps(manifest, indent=1))
    return 0


def _cmd_migrate(app, args) -> int:
    from .migrations import Migrator, new_migration

    if args.action == "new":
        path = new_migration(args.dir, args.name)
        print(f"created {path}")
        return 0
    db = getattr(app, "state_db", None)
    if db is None:
        asyncio.run(app.startup())
        db = getattr(app, "state_db", None)
    if db is None:
        print("error: no database (register DatabasePlugin first)")
        return 1

    async def go():
        m = Migrator(db, args.dir)
        if args.action == "status":
            rep = await m.status()
            print(f"applied: {rep['applied'] or 'none'}")
            for v, name in rep["pending"]:
                print(f"  pending {v:04d} {name}")
            return 0
        if args.action == "up":
            done = await m.up(args.to)
            print(f"applied: {done or 'already current'}")
            return 0
        done = await m.down(args.steps)
        print(f"reverted: {done or 'nothing to revert'}")
        return 0

    try:
        return asyncio.run(go())
    finally:
        try:
            asyncio.run(app.shutdown())
        except Exception:
            pass


def _cmd_worker(app, args) -> int:
    from .queue import run_worker

    async def go():
        await app.startup()
        print(f"worker draining queue '{args.queue}' (Ctrl+C to stop)…")
        try:
            n = await run_worker(app, poll=args.poll)
        except KeyboardInterrupt:
            n = -1
        print(f"worker done ({n} jobs)" if n >= 0 else "worker stopped")
        return 0

    try:
        return asyncio.run(go())
    finally:
        try:
            asyncio.run(app.shutdown())
        except Exception:
            pass


if __name__ == "__main__":
    main()
