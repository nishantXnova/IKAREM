# Launch kit — copy, paste, post. Numbers verified 2026-10-14.

Pre-flight (all true today, re-verify before posting):
- `pip install ikarem` → 1.4.0 live on PyPI
- 491 tests green across framework + 6 showcases + 3 extensions
- Footprint (2026-10-08): 1 package / 905 kB / 146 ms cold import
  vs FastAPI 11 / 14 MB, Django 4 / 31 MB (`bench/RESULTS.md`)
- Pentest recall 7/7 with token, 0 false positives (vuln gym in-tree)
- Site live: https://ikarem.vercel.app/ (+ /extensions/* pages)

Do NOT claim: extensions on PyPI (pending publishers — path-installs
until tags land), public demo URL (no deploy yet), "faster than X"
(empty-route rows lose honestly; I/O dominates anyway).

---

## Show HN: IKAREM — zero-dependency Python ASGI framework

Title: `Show HN: IKAREM – Python ASGI framework with zero required dependencies`

Body:

I built the backend framework I wanted to exist: FastAPI-style DX
(DI, validation, OpenAPI, JWT auth, websockets, background jobs) on a
core that is pure stdlib + ASGI. `pip install ikarem` pulls exactly 1
package (905 kB). No pydantic, no click, no starlette underneath.

What it does differently:

- Handler signatures compile once into plans — per-request work is
  dict lookups, no reflection. `ikarem check` statically audits
  handlers (f-string SQL, blocking calls, duplicate routes, unauthenticated
  mutating routes) before traffic.
- Every route is also an MCP tool (`ikarem mcp`), plus SSE responses,
  NISH negotiation, durable queues + cron + versioned migrations.
- Security is graded, not claimed: `ikarem audit` runs 11 live controls
  (it forges JWTs at audit time to prove the token policy).
- Three installable extensions: a self-pentest scanner (fires real
  auth-bypass/XSS/SQLi probes at your routes, 7/7 recall on a planted
  vuln gym), OAuth2 login (PKCE, refresh rotation with theft detection),
  and verified SQLite backups (every run drills the restore).

Honest tradeoffs: no ORM (raw SQL strategies + validated schemas is the
answer), ecosystem is me + whoever shows up first, empty-route
microbenchmarks lose to frameworks that do less per request (I publish
the losing rows too: bench/RESULTS.md). 491 tests, MIT.

Repo: https://github.com/nishantXnova/IKAREM — happy to answer
anything, especially the parts you think are wrong.

---

## r/Python version

Title: `IKAREM 1.4.0 – zero-dependency ASGI framework + self-pentesting extension`

Shorter cut of the above, same numbers, plus:

- `pip install ikarem`, `pip install ikarem[postgres]` etc. for extras
- Extensions are stdlib-only too: pentest, OAuth2, backups
- Docs: https://ikarem.vercel.app/ — roast welcome, specific beats general

Post Tuesday–Thursday morning US time. Answer every comment for 48h.
Do not argue with "just use FastAPI" — agree where it's true (ecosystem),
state the one reason to switch (zero-dep + static audit), move on.
