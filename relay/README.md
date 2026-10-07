# Relay — team incident + status hub on IKAREM

Watches services, opens incidents, pages responders, and proves the
framework while doing it. The biggest IKAREM showcase: 28 routes plus
system endpoints, every subsystem earning its place.

```bash
pip install -r requirements.txt
python -m pytest relay/tests -q   # from the IKAREM repo root
uvicorn app:app                   # demo login: admin@ex.co / admin1234
```

What it exercises: sessions + CSRF (browser), JWT roles (API),
API keys (ingesters, hashed at rest, shown once), SpikeManager (ingest
flood lane), nitro rollups, Room live feed (`/ws/feed`), durable queue
notifications, cron probes with auto-incidents, MCP tools (`/mcp`),
NISH mode, Blueprints-free flat routes with a static `check` that stays
green. `/debug/ikarem` renders the framework's own pulse live:
SpikeManager snapshot, nitro stats, route/tool/audit counts.

Deploy: `docker compose up --build` (Postgres) with `IKAREM_SESSION_SECRET`
and `IKAREM_AUTH_SECRET` set. Probes run every 120s via the scheduler;
drain notifications with `ikarem worker app:app`.
