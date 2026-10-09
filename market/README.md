# BAZAAR — Town square market

The classic Flask/FastAPI tutorial app (shops, carts, checkout, reviews,
admin), built on stock IKAREM instead. One SQLite file, zero new deps.

## Run

```bash
pip install -e ".[server]"
uvicorn market.app:app --port 8001
# demo tabs: buyer@bazaar.local / buyer1234
#            seller@bazaar.local / seller1234
#            admin@bazaar.local / admin1234
```

Or: `ikarem run market.app:app --port 8001`, `ikarem check market.app:app`.

## What is inside

| Surface | Routes |
|---|---|
| Square | `GET /` — search, stall/sort filters, pages, catalog grid, live sold wire |
| Goods | `/p/{id}` — portraits, stock states, verdicts, gated reviews, wishlist |
| Basket + till | `/cart`, `POST /checkout` — **one transaction**: wallet + stock + coupon + order, or nothing moves. `Idempotency-Key` replays, never double-rings |
| Parcels | `/orders`, `/orders/{code}` — placed→paid→packed→shipped→delivered trail, seller/admin advance, buyer cancel + refund + restock |
| Wallet | `/wallet` — house tab + play-money top-ups (new tabs start at $200) |
| Seller desk | `/sell` — takings chart, pack list, drafts → submit for review |
| Warden | `/admin` — chalk queue (approve/reject), coupon cutting, badge grants, CSV export |
| Wire | `ws://…/ws/ticker` — every till rings the public tape live |
| API + NISH | `/api/*` negotiated from day one; `/explorer` index; `/openapi.nish` |

Auth is triple (session / Bearer JWT / `X-API-Key`); RBAC is one helper,
`need_role(req, …)`, enforced against the user row so all three auth paths
obey the same badges. HTML writes carry double-submit CSRF; `/api/*` is
exempt and token-guarded.

## The till, precisely

`run_checkout` holds a single `async with db.transaction()`: unknown goods,
dead coupons, short stock, and thin wallets each abort with a 400 naming
the fix — wallet, stock, coupon counter, order, items, events, and basket
clear all commit together.

## Tests

```bash
python -m pytest market/tests -q   # 14 flows, isolated tmp SQLite per test
```
