# IKAREM extensions — freemium power tools

Core rule: `ikarem audit` (read-only grading, 11 controls) stays built in
forever. Everything here **actively attacks your own app** in-process via
`TestClient` — no server, no network, no third-party service.

## Model

- Basics built in: `ikarem audit`, `ikarem check`, rate limits, security
  headers, JWT proofs. Zero deps, always available.
- Power tools installable: each folder here is a standalone pip package
  (`ikarem-<name>`, import `ikarem_<name>`). Same plugin protocol
  (`name/requires/priority/register` + hooks), own versioning, own breakage.
- No core changes: extensions live here, never in `ikarem/`. Lazy imports
  only; a missing extra names the exact `pip install` line.
- Removal path: every extension README states the native equivalent
  (what you keep if you uninstall it).

## Shipped

| Extension | What it does | Install |
|---|---|---|
| `ikarem-pentest` | Active self-pentest: fires auth-bypass, XSS, SQLi, open-redirect, body-cap, traceback probes at your routes over `TestClient` | `pip install ./extensions/ikarem-pentest` |
| `ikarem-oauth` | Refresh tokens (rotation, reuse kills chain, revocation) + OAuth2 code-flow login (Google/GitHub presets) | `pip install ./extensions/ikarem-oauth` |
| `ikarem-backup` | SQLite online dumps, restore drills, retention, pluggable upload | `pip install ./extensions/ikarem-backup` |

## Roadmap (claim one)

| Extension | Why it's power-tool, not core |
|---|---|
| `ikarem-backup` | Scheduled SQLite dumps + S3 upload + restore drill; core has no I/O opinions |
| `ikarem-gdpr` | User-data export/erase across your tables; schema-specific, can't live in core |
| `ikarem-load` | Sustained-load runner with budgets (wraps `bench/load.py` as a pass/fail gate) |
| `ikarem-admin` | Browsable CRUD over `app.resource()`; UI weight core must never carry |
| `ikarem-mail` | SMTP lazy extra + queue task; needs credentials core must never see |

## Checklist (every extension)

1. Standalone `pyproject.toml` (`ikarem-<name>`, dep: `ikarem`, extras lazy).
2. Hermetic tests (fresh app per test, unique paths, no shared state).
3. Findings carry evidence + remedy, never swallow silently.
4. Native equivalent documented (removal path).
