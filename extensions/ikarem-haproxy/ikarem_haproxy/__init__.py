"""IKAREM-aware HAProxy edge: one config generated from the compiled plans.

HAProxy is the reverse proxy ecosystem's reference implementation: TLS
termination, health-checked backends, per-IP stick tables, and reloads
without dropping connections. It knows nothing about IKAREM, so this
extension teaches it the one thing it cannot infer from a port number:
the app's route map.

The map comes from the same compiled plans that serve traffic
(``ikarem.diagram.build_diagram``), so every backend, ACL and health
check matches the running app:

* one backend per IKAREM blueprint/route group, with its real path
  prefixes as ``path_beg`` ACLs (a blueprint may own several prefixes,
  and the group that owns ``/`` becomes the default backend);
* ``/healthz`` checks the probe IKAREM mounts itself;
* ``X-Forwarded-For`` is *overwritten* with the real peer address, which
  is what makes IKAREM's per-IP rate limiter trustworthy behind a proxy;
* WebSocket routes get the tunnel timeout they need;
* ``/static`` can carry opt-in cache headers at the edge.

Nothing here parses JWTs: auth stays in IKAREM, where the app's own
compiled plans enforce it. This extension only moves bytes faster and
sheds abusive traffic before it reaches Python.

Removal path: delete the generated ``haproxy.cfg`` and run
``ikarem run myapp:app`` directly. See README.md for the native
equivalents of TLS, rate limits and health checks.
"""

from __future__ import annotations

import importlib
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

__version__ = "0.1.0"

__all__ = [
    "EdgeConfig",
    "Zone",
    "HAPROXY_REMEDY",
    "build_edge",
    "check_config",
    "find_haproxy",
    "load_app",
    "render",
    "run_command",
    "serve",
    "write_config",
]

HAPROXY_REMEDY = (
    "HAProxy 3.0+ was not found on PATH: install it "
    "(winget install haproxy | brew install haproxy | apt install haproxy), "
    "set IKAREM_HAPROXY=/path/to/haproxy, or pass --haproxy /path/to/haproxy"
)

_DEFAULT_UPSTREAM = "127.0.0.1:8000"
_UPSTREAM_RE = re.compile(r"^(\[[0-9a-fA-F:.]+\]|[^:\s]+):(\d{1,5})$")


@dataclass(frozen=True)
class Zone:
    """One backend per IKAREM blueprint/route group, matched by path prefix."""

    label: str
    name: str
    prefixes: tuple[str, ...]
    matches_root: bool
    routes: int
    auth: int
    websockets: int


@dataclass(frozen=True)
class EdgeConfig:
    """Everything needed to render a config. No I/O, no clock, no randomness."""

    title: str
    zones: tuple[Zone, ...]
    source: str = ""
    default_zone: str = "ikarem"
    listen: str = "0.0.0.0:8080"
    upstreams: tuple[str, ...] = (_DEFAULT_UPSTREAM,)
    health_path: str = "/healthz"
    maxconn: int = 4000
    rate_limit: int = 0
    static_cache: int = 0
    tls_cert: str | None = None
    bind_https: str = ":443"
    bind_http: str = ":80"
    stats: bool = False
    stats_bind: str = "127.0.0.1:8404"
    hsts: bool = True
    single_backend: bool = False
    warnings: tuple[str, ...] = ()

    @property
    def routes(self) -> int:
        return sum(z.routes for z in self.zones)

    @property
    def auth_routes(self) -> int:
        return sum(z.auth for z in self.zones)

    @property
    def websockets(self) -> int:
        return sum(z.websockets for z in self.zones)

    @property
    def backend_name(self) -> str:
        return self.default_zone


def _slug(text: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "_", text.strip("/").lower()).strip("_")
    return cleaned or "root"


def _prefix_of(path: str) -> str:
    segment = (path or "/").strip("/").split("/", 1)[0]
    return f"/{segment}" if segment else ""


def _normalize_upstreams(upstreams: Sequence[str] | str | None) -> tuple[str, ...]:
    if upstreams is None:
        return (_DEFAULT_UPSTREAM,)
    raw: list[str] = []
    for part in [upstreams] if isinstance(upstreams, str) else upstreams:
        raw.extend(piece.strip() for piece in str(part).split(","))
    out = [part for part in raw if part]
    if not out:
        raise ValueError(
            f"no upstream given: pass host:port such as {_DEFAULT_UPSTREAM!r} "
            "(comma-separate several workers to load balance)"
        )
    for item in out:
        match = _UPSTREAM_RE.match(item)
        if match is None:
            raise ValueError(
                f"invalid upstream {item!r}: expected host:port "
                f"(for example {_DEFAULT_UPSTREAM!r}); IPv6 must be bracketed like [::1]:8000"
            )
        port = int(match.group(2))
        if not 1 <= port <= 65535:
            raise ValueError(f"invalid upstream port in {item!r}: ports are 1-65535")
    return tuple(out)


def _read_plans(app: Any) -> tuple[list[Any], str]:
    from ikarem.diagram import build_diagram

    graph = build_diagram(app)
    routes = [n for n in graph.nodes if n.kind in ("route", "ws")]
    return routes, graph.title


def build_edge(
    app: Any,
    *,
    title: str | None = None,
    source: str = "",
    listen: str = "0.0.0.0:8080",
    upstream: Sequence[str] | str | None = None,
    health_path: str = "/healthz",
    maxconn: int = 4000,
    rate_limit: int = 0,
    static_cache: int = 0,
    tls_cert: str | os.PathLike[str] | None = None,
    bind_https: str = ":443",
    bind_http: str = ":80",
    stats: bool = False,
    stats_bind: str = "127.0.0.1:8404",
    hsts: bool = True,
    single_backend: bool = False,
) -> EdgeConfig:
    """Derive an edge config from the app's compiled route table.

    Raises ``ValueError``/``FileNotFoundError`` with a fix in the message;
    never guesses around a mistake that would silently weaken the edge.
    """
    if maxconn < 1:
        raise ValueError(f"maxconn must be >= 1 (got {maxconn})")
    if rate_limit < 0:
        raise ValueError(f"rate_limit must be >= 0 requests/10s (got {rate_limit}); 0 disables it")
    if static_cache < 0:
        raise ValueError(f"static_cache must be >= 0 seconds (got {static_cache})")
    if not health_path.startswith("/"):
        raise ValueError(f"health_path must start with '/': got {health_path!r}")

    cert: str | None = None
    if tls_cert is not None:
        path = Path(tls_cert)
        if not path.is_file():
            raise FileNotFoundError(
                f"TLS certificate bundle not found: {path} "
                "(HAProxy wants one PEM containing the certificate chain and the private key; "
                "create it with: cat fullchain.pem privkey.pem > site.pem)"
            )
        cert = str(path.resolve())

    routes, graph_title = _read_plans(app)
    upstreams = _normalize_upstreams(upstream)
    warnings: list[str] = []

    if health_path != "/healthz" and not any(r.path == health_path for r in routes):
        warnings.append(
            f"health check {health_path} is not a registered route: "
            "point --health-path at a real probe or drop the flag to use IKAREM's /healthz"
        )
    if static_cache and not any(_prefix_of(r.path) == "/static" for r in routes):
        warnings.append(
            "static_cache is set but the app has no /static route: the edge rule will "
            "match nothing; drop --static-cache or mount static files under /static"
        )

    if single_backend:
        zone = Zone(
            label="all routes",
            name="ikarem",
            prefixes=(),
            matches_root=True,
            routes=len(routes),
            auth=sum(1 for r in routes if r.auth),
            websockets=sum(1 for r in routes if r.kind == "ws"),
        )
        return EdgeConfig(
            title=title or graph_title,
            zones=(zone,),
            source=source,
            default_zone="ikarem",
            listen=listen,
            upstreams=upstreams,
            health_path=health_path,
            maxconn=maxconn,
            rate_limit=rate_limit,
            static_cache=static_cache,
            tls_cert=cert,
            bind_https=bind_https,
            bind_http=bind_http,
            stats=stats,
            stats_bind=stats_bind,
            hsts=hsts,
            single_backend=True,
            warnings=tuple(warnings),
        )

    groups: dict[str, dict[str, Any]] = {}
    for node in routes:
        label = getattr(node, "group", "") or "app"
        bucket = groups.setdefault(
            label,
            {"prefixes": set(), "root": False, "routes": 0, "auth": 0, "ws": 0},
        )
        prefix = _prefix_of(node.path)
        if prefix:
            bucket["prefixes"].add(prefix)
        else:
            bucket["root"] = True
        bucket["routes"] += 1
        bucket["auth"] += 1 if node.auth else 0
        bucket["ws"] += 1 if node.kind == "ws" else 0

    used: dict[str, int] = {}
    zones_list: list[Zone] = []
    for label in sorted(
        groups, key=lambda name: (-max((len(p) for p in groups[name]["prefixes"]), default=0), name)
    ):
        slug = _slug(label)
        used[slug] = used.get(slug, 0) + 1
        name = f"zone_{slug}" if used[slug] == 1 else f"zone_{slug}_{used[slug]}"
        b = groups[label]
        zones_list.append(
            Zone(
                label=label,
                name=name,
                prefixes=tuple(sorted(b["prefixes"])),
                matches_root=bool(b["root"]),
                routes=b["routes"],
                auth=b["auth"],
                websockets=b["ws"],
            )
        )
    if not zones_list:
        zones_list.append(Zone("all routes", "ikarem", (), True, 0, 0, 0))

    # Unmatched paths (404s) go to the group that owns "/" - or the biggest
    # group when the app has no root route. Deterministic either way.
    default = next((z.name for z in zones_list if z.matches_root), None)
    if default is None:
        default = max(zones_list, key=lambda z: (z.routes, z.name)).name

    return EdgeConfig(
        title=title or graph_title,
        zones=tuple(zones_list),
        source=source,
        default_zone=default,
        listen=listen,
        upstreams=upstreams,
        health_path=health_path,
        maxconn=maxconn,
        rate_limit=rate_limit,
        static_cache=static_cache,
        tls_cert=cert,
        bind_https=bind_https,
        bind_http=bind_http,
        stats=stats,
        stats_bind=stats_bind,
        hsts=hsts,
        single_backend=False,
        warnings=tuple(warnings),
    )


def _frontend(lines: list[str], cfg: EdgeConfig, name: str, bind: str, *, tls: bool) -> None:
    lines.append(f"frontend {name}")
    lines.append(f"    bind {bind}")
    if tls:
        lines.append("    # HTTP/2 to browsers; upstream stays HTTP/1.1 (IKAREM speaks h11)")
    if cfg.hsts and tls:
        lines.append(
            '    http-response set-header Strict-Transport-Security "max-age=31536000; includeSubDomains"'
        )
    if cfg.rate_limit:
        lines.append("    # Edge shed: same 429 shape as IKAREM's RateLimitMiddleware, before Python")
        lines.append("    stick-table type ip size 100k expire 30s store http_req_rate(10s)")
        lines.append("    http-request track-sc0 src")
        lines.append(
            "    http-request deny deny_status 429 content-type application/json "
            f'string \'{{"detail":"rate limit exceeded"}}\' '
            f"hdr Retry-After 10 if {{ sc_http_req_rate(0) gt {cfg.rate_limit} }}"
        )
    lines.append("    # Trusted client IP: overwrite (not append) so spoofed values cannot")
    lines.append("    # pick their own rate-limit bucket inside IKAREM. Only safe while this")
    lines.append("    # edge is the sole ingress to the upstream port.")
    lines.append("    http-request set-header X-Forwarded-For %[src]")
    lines.append("    http-request set-header X-Real-IP %[src]")
    lines.append(f"    http-request set-header X-Forwarded-Proto {'https' if tls else 'http'}")
    lines.append("    http-request set-header X-Forwarded-Host %[req.hdr(Host)]")
    if cfg.static_cache:
        lines.append("    # Opt-in edge cache for static files (replaces the app's Cache-Control)")
        lines.append("    acl edge_static path_beg /static")
        lines.append(
            "    http-response set-header Cache-Control "
            f'"public, max-age={cfg.static_cache}, immutable" if edge_static'
        )
    if not cfg.single_backend:
        for zone in cfg.zones:
            if zone.prefixes:
                lines.append(f"    acl {zone.name} path_beg {' '.join(zone.prefixes)}")
            if zone.matches_root:
                lines.append(f"    acl {zone.name} path /")
        for zone in cfg.zones:
            if zone.prefixes or zone.matches_root:
                lines.append(f"    use_backend {zone.name} if {zone.name}")
    lines.append(f"    default_backend {cfg.backend_name}")
    lines.append("")


def _backend(lines: list[str], cfg: EdgeConfig, zone: Zone) -> None:
    facts = [f"{zone.routes} route" + ("" if zone.routes == 1 else "s")]
    if zone.auth:
        facts.append(f"{zone.auth} auth")
    if zone.websockets:
        facts.append(f"{zone.websockets} WS")
    lines.append(f"backend {zone.name}")
    lines.append(f"    # {zone.label} - " + " - ".join(facts))
    lines.append("    mode http")
    lines.append("    balance roundrobin")
    lines.append(
        "    # IKAREM probes are mounted at startup; a 200 means the app, not just the socket, is up"
    )
    lines.append(f"    option httpchk GET {cfg.health_path}")
    lines.append("    http-check expect status 200")
    lines.append("    http-reuse safe")
    for index, address in enumerate(cfg.upstreams, start=1):
        lines.append(f"    server app{index} {address} check")
    lines.append("")


def render(cfg: EdgeConfig) -> str:
    """Render a complete ``haproxy.cfg``. Deterministic for a given config."""
    lines: list[str] = []
    lines.append(f"# Generated by ikarem-haproxy {__version__} - do not edit by hand")
    if cfg.source:
        lines.append(f"# source: {cfg.source}")
    lines.append(f"# app: {cfg.title}")
    lines.append(
        f"# {len(cfg.zones)} zones | {cfg.routes} routes"
        f" ({cfg.websockets} WS) | {cfg.auth_routes} auth | "
        f"{len(cfg.upstreams)} upstream{'s' if len(cfg.upstreams) != 1 else ''}"
    )
    lines.append("# Regenerate: ikarem-haproxy config <module:app> -o haproxy.cfg")
    lines.append("# Auth stays in IKAREM (compiled plans); HAProxy never inspects tokens.")
    for warning in cfg.warnings:
        lines.append(f"# WARNING: {warning}")
    lines.append("")
    lines.append("global")
    lines.append(f"    maxconn {cfg.maxconn}")
    lines.append("    log stdout format raw local0")
    if cfg.tls_cert:
        lines.append("    ssl-default-bind-options ssl-min-ver TLSv1.2 no-tls-tickets")
    lines.append("")
    lines.append("defaults")
    lines.append("    mode http")
    lines.append("    log global")
    lines.append("    option httplog")
    lines.append("    option dontlognull")
    lines.append("    option forwardfor if-none")
    lines.append("    option redispatch")
    lines.append("    retries 3")
    lines.append("    timeout connect 5s")
    lines.append("    timeout client 30s")
    lines.append("    timeout server 30s")
    lines.append("    timeout http-request 10s")
    lines.append("    timeout http-keep-alive 60s")
    lines.append("    timeout queue 30s")
    lines.append("    # WebSocket routes: without this, 101 upgrades die at the client timeout")
    lines.append("    timeout tunnel 1h")
    lines.append("    default-server inter 2s fastinter 500ms downinter 5s rise 2 fall 3")
    lines.append("")

    if cfg.tls_cert:
        lines.append("frontend ikarem_http")
        lines.append(f"    bind {cfg.bind_http}")
        lines.append("    http-request redirect scheme https code 308")
        lines.append("")
        _frontend(
            lines,
            cfg,
            "ikarem_https",
            f"{cfg.bind_https} ssl crt {cfg.tls_cert} alpn h2,http/1.1",
            tls=True,
        )
    else:
        _frontend(lines, cfg, "ikarem_in", cfg.listen, tls=False)

    if cfg.stats:
        lines.append("listen stats")
        lines.append(f"    bind {cfg.stats_bind}")
        lines.append("    mode http")
        lines.append("    stats enable")
        lines.append("    stats uri /")
        lines.append("    stats refresh 10s")
        lines.append("")

    for zone in cfg.zones:
        _backend(lines, cfg, zone)

    return "\n".join(lines).rstrip() + "\n"


def write_config(cfg: EdgeConfig, path: str | os.PathLike[str]) -> Path:
    """Write the config atomically (temp file in the same dir, then replace)."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(render(cfg), encoding="utf-8", newline="\n")
    os.replace(tmp, target)
    return target


def find_haproxy(explicit: str | os.PathLike[str] | None = None) -> str | None:
    """Locate the HAProxy binary: explicit path, ``IKAREM_HAPROXY``, then PATH."""
    if explicit:
        return str(explicit)
    env = os.environ.get("IKAREM_HAPROXY")
    if env:
        return env
    return shutil.which("haproxy")


def check_config(
    path: str | os.PathLike[str],
    haproxy: str | None = None,
) -> tuple[int, str]:
    """Run ``haproxy -c -f`` and return ``(returncode, combined output)``."""
    exe = find_haproxy(haproxy)
    if exe is None:
        raise FileNotFoundError(HAPROXY_REMEDY)
    proc = subprocess.run(
        [exe, "-c", "-f", str(path)],
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def run_command(
    path: str | os.PathLike[str],
    haproxy: str | None = None,
    *extra: str,
) -> list[str]:
    """Build the foreground command (``-db``) used by the CLI's ``run``."""
    exe = find_haproxy(haproxy)
    if exe is None:
        raise FileNotFoundError(HAPROXY_REMEDY)
    return [exe, "-f", str(path), "-db", *extra]


def serve(cmd: Sequence[str]) -> int:
    """Run a prepared HAProxy command in the foreground. Ctrl-C returns 130.

    Build the command with :func:`run_command` so binary discovery and the
    install remedy happen exactly once, before anything starts.
    """
    try:
        return subprocess.call(list(cmd))
    except KeyboardInterrupt:
        return 130


def load_app(target: str) -> Any:
    """Import ``module:attr`` (or ``module``) and return the app object."""
    module_name, _, attr = target.partition(":")
    attr = attr or "app"
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise ImportError(
            f"cannot import {module_name!r}: {exc}. Run from the project root "
            "(the directory containing your app module) and check the module name"
        ) from exc
    try:
        app = getattr(module, attr)
    except AttributeError as exc:
        raise AttributeError(
            f"{module_name!r} has no attribute {attr!r}: use module:attr, for example {module_name}:app"
        ) from exc
    if not hasattr(app, "router"):
        raise TypeError(
            f"{target!r} is not an IKAREM app (no .router): pass module:app pointing at an Ikarem instance"
        )
    return app
