"""``ikarem-haproxy`` command line: config, check, run, zones.

The generator is pure (app in, text out); this module is the only place
that touches the filesystem, subprocesses and exit codes. Errors always
name the remedy and exit ``2`` for "your input is wrong" and ``1`` for
"HAProxy rejected the config".
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path
from typing import Sequence

from . import (
    EdgeConfig,
    build_edge,
    check_config,
    load_app,
    render,
    run_command,
    serve,
    write_config,
)

__all__ = ["main"]


def _add_build_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "target",
        nargs="?",
        help="module:attr pointing at the Ikarem app (for example ledger.app:app)",
    )
    parser.add_argument(
        "-o", "--output", default="haproxy.cfg", help="config file to write (default: haproxy.cfg)"
    )
    parser.add_argument("--title", default=None, help="label in the generated header (default: app title)")
    parser.add_argument("--listen", default="0.0.0.0:8080", help="plain-HTTP bind (default: 0.0.0.0:8080)")
    parser.add_argument(
        "--upstream",
        action="append",
        default=None,
        metavar="HOST:PORT",
        help="IKAREM address; repeat or comma-separate for several workers (default: 127.0.0.1:8000)",
    )
    parser.add_argument("--health-path", default="/healthz", help="probe path (default: /healthz)")
    parser.add_argument("--maxconn", type=int, default=4000, help="global connection ceiling (default: 4000)")
    parser.add_argument(
        "--rate-limit",
        type=int,
        default=0,
        metavar="N",
        help="per-IP request cap per 10s at the edge; 0 disables (default: 0)",
    )
    parser.add_argument(
        "--static-cache",
        type=int,
        default=0,
        metavar="SECONDS",
        help="Cache-Control max-age for the /static zone; 0 keeps app headers (default: 0)",
    )
    parser.add_argument("--tls-cert", default=None, help="PEM bundle (chain + key) to terminate TLS")
    parser.add_argument(
        "--bind-https", default=":443", help="TLS bind when --tls-cert is set (default: :443)"
    )
    parser.add_argument(
        "--bind-http", default=":80", help="redirect bind when --tls-cert is set (default: :80)"
    )
    parser.add_argument("--stats", action="store_true", help="expose the HAProxy stats page")
    parser.add_argument("--stats-bind", default="127.0.0.1:8404", help="stats bind (default: 127.0.0.1:8404)")
    parser.add_argument(
        "--no-hsts", action="store_true", help="do not add Strict-Transport-Security with TLS"
    )
    parser.add_argument(
        "--single-backend",
        action="store_true",
        help="skip per-prefix backends; one upstream for every path",
    )


def _build(args: argparse.Namespace, target: str | None) -> EdgeConfig:
    if not target:
        raise ValueError(
            "no app given: pass module:app (for example ledger.app:app) "
            "or use --config with an existing haproxy.cfg"
        )
    app = load_app(target)
    return build_edge(
        app,
        title=args.title,
        source=target,
        listen=args.listen,
        upstream=args.upstream,
        health_path=args.health_path,
        maxconn=args.maxconn,
        rate_limit=args.rate_limit,
        static_cache=args.static_cache,
        tls_cert=args.tls_cert,
        bind_https=args.bind_https,
        bind_http=args.bind_http,
        stats=args.stats,
        stats_bind=args.stats_bind,
        hsts=not args.no_hsts,
        single_backend=args.single_backend,
    )


def _warn(cfg: EdgeConfig) -> None:
    for warning in cfg.warnings:
        print(f"warning: {warning}", file=sys.stderr)


def _cmd_config(args: argparse.Namespace) -> int:
    cfg = _build(args, args.target)
    _warn(cfg)
    if args.output == "-":
        sys.stdout.write(render(cfg))
        return 0
    path = write_config(cfg, args.output)
    upstreams = ", ".join(cfg.upstreams)
    print(
        f"wrote {path} ({len(cfg.zones)} zones, {cfg.routes} routes, "
        f"{len(cfg.upstreams)} upstream{'s' if len(cfg.upstreams) != 1 else ''}: {upstreams})"
    )
    if args.verify:
        return _verify(path, args.haproxy)
    return 0


def _verify(path: Path, haproxy: str | None) -> int:
    code, output = check_config(path, haproxy)
    if code == 0:
        print(f"haproxy -c: {path} is valid")
        return 0
    print(f"haproxy -c rejected {path}:\n{output}", file=sys.stderr)
    return 1


def _cmd_check(args: argparse.Namespace) -> int:
    if args.config:
        path = Path(args.config)
        if not path.is_file():
            print(
                f"error: config file not found: {path} (generate one with: ikarem-haproxy config <module:app>)",
                file=sys.stderr,
            )
            return 2
    else:
        cfg = _build(args, args.target)
        _warn(cfg)
        with tempfile.TemporaryDirectory(prefix="ikarem-haproxy-") as tmp:
            path = write_config(cfg, Path(tmp) / "haproxy.cfg")
            return _verify(path, args.haproxy)
    return _verify(path, args.haproxy)


def _cmd_run(args: argparse.Namespace) -> int:
    if args.config:
        path = Path(args.config)
        if not path.is_file():
            print(
                f"error: config file not found: {path} (generate one with: ikarem-haproxy config <module:app>)",
                file=sys.stderr,
            )
            return 2
    else:
        cfg = _build(args, args.target)
        _warn(cfg)
        path = write_config(cfg, args.output)
        print(f"wrote {path} ({len(cfg.zones)} zones, {cfg.routes} routes)")
    extra = tuple(args.haproxy_args or ())
    cmd = run_command(path, args.haproxy, *extra)
    print(f"starting HAProxy: {path}  (Ctrl-C to stop)")
    return serve(cmd)


def _cmd_zones(args: argparse.Namespace) -> int:
    cfg = _build(args, args.target)
    _warn(cfg)
    print(f"{'BLUEPRINT':<16} {'PATHS':<40} {'BACKEND':<18} {'ROUTES':>6} {'AUTH':>5} {'WS':>3}")
    for zone in cfg.zones:
        paths = " ".join((*zone.prefixes, "/" if zone.matches_root else ""))
        if len(paths) > 39:
            paths = paths[:36] + "..."
        print(
            f"{zone.label[:16]:<16} {paths:<40} {zone.name:<18} {zone.routes:>6} "
            f"{zone.auth:>5} {zone.websockets:>3}"
        )
    return 0


def _add_haproxy_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--haproxy", default=None, help="path to the HAProxy binary (or set IKAREM_HAPROXY)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ikarem-haproxy",
        description=(
            "Generate a complete HAProxy edge from an IKAREM app's compiled plans: "
            "per-prefix backends, /healthz checks, trustworthy client IPs, WebSocket "
            "tunnels, optional TLS and edge rate limiting. Config only - auth stays in IKAREM."
        ),
    )
    parser.add_argument("--version", action="version", version="%(prog)s 0.1.0")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("config", help="generate a haproxy.cfg")
    _add_build_options(p)
    p.add_argument("--verify", action="store_true", help="run haproxy -c on the result")
    _add_haproxy_flag(p)
    p.set_defaults(func=_cmd_config)

    p = sub.add_parser("check", help="validate a generated or existing config")
    _add_build_options(p)
    p.add_argument("--config", default=None, help="validate this file instead of generating one")
    _add_haproxy_flag(p)
    p.set_defaults(func=_cmd_check)

    p = sub.add_parser("run", help="generate (unless --config) and run HAProxy in the foreground")
    _add_build_options(p)
    p.add_argument("--config", default=None, help="run this file instead of generating one")
    _add_haproxy_flag(p)
    p.set_defaults(func=_cmd_run)

    p = sub.add_parser("zones", help="print the prefix -> backend table")
    _add_build_options(p)
    p.set_defaults(func=_cmd_zones)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    raw = list(sys.argv[1:] if argv is None else argv)
    # Everything after a bare ``--`` goes to HAProxy untouched (a REMAINDER
    # positional would swallow this command's own flags instead).
    passthrough: list[str] = []
    if "--" in raw:
        cut = raw.index("--")
        passthrough = raw[cut + 1 :]
        raw = raw[:cut]
    args = parser.parse_args(raw)
    args.haproxy_args = passthrough
    try:
        return int(args.func(args))
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except (ImportError, AttributeError, TypeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover - exercised through __main__
    raise SystemExit(main())
