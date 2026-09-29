# Cadence

Small habits, kept daily. A habit tracker built on [IKAREM](../README.md):
session auth, CSRF-protected forms, a real streak engine, SVG-free CSS
heatmaps, CSV export, and a JSON API with auto-generated OpenAPI docs.

## Run

```bash
pip install -r requirements.txt
cp .env.example .env   # set IKAREM_SESSION_SECRET + IKAREM_AUTH_SECRET!
pytest -q              # full suite
uvicorn cadence.app:app
```

Open `/` (dashboard), `/habits` (manage), `/docs` (API reference).

## Use

1. Register, then **+ New habit** — name it, pick a colour and a weekly target.
2. Each day, hit the circle next to a habit. It works without JavaScript;
   with JavaScript it toggles in place.
3. The detail page shows current/longest streak, this week vs target,
   a 12-week heatmap, and backfill for days you forgot to log
   (the future is rejected — no time travellers).

Streak rule: consecutive days ending today, or yesterday if today
isn't done yet. Archive keeps history; delete removes it.

## API

| Method | Path | Notes |
|---|---|---|
| GET | `/api/summary` | today + per-habit stats |
| GET/POST | `/api/habits` | list / create (`HabitIn` JSON) |
| GET/PUT/DELETE | `/api/habits/{id}` | PUT takes a full habit body |
| POST | `/api/habits/{id}/toggle` | `{"day": "YYYY-MM-DD"}` optional, defaults today |
| GET | `/api/history?days=84` | check-in days |
| GET | `/api/csrf` | CSRF token for unsafe JSON calls (`X-CSRF-Token`) |

## Config (env)

| Var | Default | Notes |
|---|---|---|
| `IKAREM_SESSION_SECRET` | dev fallback | **change in prod** |
| `IKAREM_AUTH_SECRET` | dev fallback | **change in prod** |
| `IKAREM_DB_URL` | `sqlite:///cadence.db` | e.g. `postgresql://…` |

## Layout

```
cadence/
  app.py        routes, streak engine, HTML rendering
  static/       style.css, app.js (enhancement only), favicon.svg
  tests/        streak unit tests + full-stack flows
  Dockerfile    sqlite volume at /srv/data
```
