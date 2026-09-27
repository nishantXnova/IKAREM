# Ledger — personal finance on IKAREM

```bash
pip install -r requirements.txt
python -m pytest ledger/tests -q
python -m uvicorn ledger.app:app
```

Open http://127.0.0.1:8000 — demo login `demo@example.com` / `demo1234`
(pre-seeded with 6 months of data). HTML at `/`, JSON at `/api/*`,
interactive docs at `/docs`, MCP via `ikarem mcp ledger.app:app`.
