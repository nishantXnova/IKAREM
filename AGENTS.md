# AGENTS.md — instructions for AI coding agents working in this repo

You are working on IKAREM, a Python ASGI backend framework. Framework code
lives in `ikarem/`, the showcase app in `ledger/`, the docs site in `site/`.

## Commands (run these; do not improvise alternatives)

```bash
pip install -e ".[dev]"                    # dev environment
python -m pytest tests/ ledger/tests cadence/tests forge/tests relay/tests market/tests agent/tests extensions/ikarem-pentest/tests extensions/ikarem-oauth/tests extensions/ikarem-backup/tests -q    # full suite — must stay green
ruff check . && ruff format --check .      # lint gate (CI enforces both)
python -m ikarem.cli check ledger.app:app  # static handler audit
python bench/bench_switch.py               # only when touching the hot path
python bench/load.py                       # sustained-load proof (slow)
```

## Laws

1. **Zero-dep core is sacred.** `ikarem/` imports stdlib only. Optional integrations
   (jinja2, asyncpg, …) load lazily inside functions with an error naming the exact
   extra, e.g. `pip install ikarem[postgres]`. Never add a required dependency.
2. **Every behavior ships with a test.** Bugfix PRs include a regression test.
   New public names need `__all__` entries, doc lines, and CHANGELOG under Unreleased.
3. **No per-request reflection.** Signatures parse once in `compiled.py` plans;
   resolution is dict lookups. Prove hot-path work with the bench, not adjectives.
4. **Errors must say how to fix.** Messages name the remedy (`pip install …`,
   `pass auth_secret=`, `Did you mean: …`). Never swallow exceptions silently —
   count them, log them, or re-raise them.
5. **Conventional Commits** (`feat:`, `fix:`, `docs:`, `test:`, `bench:`, `chore:`).
   Releases and CHANGELOG are cut from these.
6. **Adapters consume ecosystems; they never become the core.** Compatibility
   can destroy the reason IKAREM exists, so bridges obey five rules: (a) one
   direction only — outside→inside, translated at registration, never
   per-request, never into `ikarem/`; (b) adapters live outside the core
   (`adapters/`), own versioning, own breakage; (c) adapted traffic runs the
   full native pipeline (`check`, compiled plans, middleware) — no
   second-class execution; (d) each adapter states the ONE thing it does
   strictly better than the default, or it gets deleted; (e) every adapter
   documents its removal path to the native equivalent.

## Architecture notes

- Request flow: `__call__` → mounts → middleware onion → `_terminal_safe`
  (renders handler errors INSIDE the pipeline so 4xx/5xx keep headers).
- `resolve_compiled(handler, request)` handles Depends/Schema/BackgroundTasks;
  string annotations resolve via `get_type_hints` (closure-variable annotations
  must stay eager — see `resources.py` note).
- `TestClient` reuses one event loop per thread: loop-bound resources (pools)
  survive across requests. One client per thread.
- SQLite connector serializes on a worker-side lock (single-lane by design);
  Postgres/MySQL pools rebuild on event-loop change.
- Never break: ASGI behavior, `__all__` names, CLI commands, response shapes
  (1.x SemVer promise).

## Where things live

- `ikarem/compiled.py` — handler plans, `check_app`, `describe_route/app`
- `ikarem/di.py`, `validation.py`, `auth.py`, `session.py`, `http.py`
- `ikarem/mcp.py` — routes-as-tools + stdio server + resources
- `ikarem/cli.py` — run/check/mcp/new/migrate/worker/inspect
- `ikarem/scaffold.py` — `ikarem new` templates (keep runnable; CI doesn't run them)
- `adapters/` — ecosystem bridges (outside the core per Law 6; reference: ASGI middleware)
- `ledger/` — showcase finance app (also the load/CI target)
- `site/` — static docs (hand-written HTML/CSS, brutal-dev style; no build step)
- `bench/` — `bench_switch.py` (honest numbers), `load.py` (sustained proof)
- `docs/` — PHASE1, MIGRATING_FROM_MERAKI, DEPLOY, PLUGINS
