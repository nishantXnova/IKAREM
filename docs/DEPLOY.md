# Deploying Ledger (or any IKAREM app)

## One command (SQLite, single node)

```bash
cd ledger
docker build -t ledger .
docker run -p 8000:8000 \
  -e IKAREM_SESSION_SECRET=$(openssl rand -hex 32) \
  -v ledger-data:/data ledger
```

## Production (Postgres, compose)

```bash
cd ledger
export IKAREM_SESSION_SECRET=$(openssl rand -hex 32)
export IKAREM_AUTH_SECRET=$(openssl rand -hex 32)
export POSTGRES_PASSWORD=$(openssl rand -hex 24)
docker compose up --build -d
```

The app reads everything from the environment (`IKAREM_*` → `app.config`):
`IKAREM_DB_URL` selects SQLite or Postgres (`postgresql://...`), no code
changes. The compose stack waits for Postgres (`service_healthy`) and the
app container health-checks `GET /readyz` — which returns 503 while the
database is unreachable, so orchestrators route around a sick node.

## Render / Fly.io / any VPS

1. Set `IKAREM_SESSION_SECRET`, `IKAREM_AUTH_SECRET`, `IKAREM_DB_URL`.
2. Serve with `uvicorn ledger.app:app --host 0.0.0.0 --port $PORT`
   (from the repo root; from `ledger/` use `app:app`).
3. Point the platform health check at `/readyz` (liveness: `/healthz`).

## Checklist before you call it production

- Secrets are generated, never `change-me-*` defaults.
- `IKAREM_DEBUG` unset/false (tracebacks stay server-side).
- Behind HTTPS (`SessionMiddleware(secure=True)` in `app.py`).
- One process per core (`--workers N`) + Postgres, not SQLite, past toy scale.
