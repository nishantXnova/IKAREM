# FORGE — Workshop OS for small teams

One big app on stock IKAREM (stdlib-only core, one SQLite file).
Jobs on the floor, cards on the board, money in the ledger, habits on the wall.

## Run

```bash
pip install -e ".[server]"
uvicorn forge.app:app          # http://127.0.0.1:8000
# demo bench: demo@forge.local / forge1234
```

Or: `ikarem run forge.app:app`, `ikarem check forge.app:app`.

## What is inside

| Surface | Routes |
|---|---|
| Dashboard | `GET /` — open cards, 7-day output, monthly spend, habit rhythm chart, spend bars, shop log (cached 30 s) |
| Jobs | list, new, detail, ship/pause, strike, remarks, file shelf, live chat |
| Board | `GET /projects/{id}/board` — todo/doing/done columns, add + move + strike cards |
| Notebook | wiki notes: pin to a job or shop-wide, edit, tear out |
| Ledger | income/expense booking, q/category/month filters, 2 CSV exports |
| Habits | 14-day grid, streaks, weekly pace, mark/unmark today |
| Crew | member roll, queue-backed invites (`ikarem worker forge.app:app` delivers) |
| Settings | day-pass JWT mint, `X-API-Key` keys (hashed at rest), revoke |
| JSON API | `/api/*` Blueprint: summary, jobs, cards, notes, entries, search — idempotent writes via `Idempotency-Key` |
| Realtime | `ws://…/ws/hall` — per-job rooms, history persisted |
| Ops | `/healthz`, `/metrics`, `/openapi.json`, `/docs`, 10-min shop digest via `@app.every` |

Auth is triple: signed-cookie sessions (HTML) + `Authorization: Bearer` JWT + `X-API-Key`.
HTML writes carry double-submit CSRF; `/api/*` is exempt and token-guarded instead.

## NISH — the viewer is a live API explorer

One switch is on in `create_app`: `app.nish_mode()`. Every JSON response
in the app therefore answers **NISH** when asked — `?format=nish` or an
`Accept` header mentioning `nish` — with content-hash ETags, so repolls
304. Install the NISH Viewer extension and open:

- `GET /explorer` — index of every NISH-ready endpoint, JSON + NISH links
- `GET /api/projects/{id}` — one job as a dossier
- `GET /api/projects/{id}/issues` — cards (`?state=` filter)
- `POST /api/projects/{id}/issues` — JSON **or NISH bodies** (`Content-Type: application/x-nish`)
- `GET /api/projects/{id}/activity` — composed timeline
- `GET /api/metrics` — rollup + rhythm tables (`[[projects]]`, `[[rhythm]]`)
- `GET /api/notifications` — derived inbox (overdue, due-soon, habits, stalled)
- `GET /openapi.nish` — the contract itself, in NISH

```bash
curl -b cookies.txt "http://127.0.0.1:8000/api/metrics?format=nish"
```

Malformed NISH bodies 400 naming the line — never silently degraded.

## Tests
```bash
python -m pytest forge/tests -q   # 15 tests (9 flows + 6 NISH), isolated tmp SQLite per test
```
