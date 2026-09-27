## What & why (1–2 lines)

## Checklist

- [ ] `ruff check . && ruff format --check .` clean
- [ ] `python -m pytest tests/ ledger/tests -q` green
- [ ] New behavior has a regression test
- [ ] Docs updated (`README.md` / `docs/`, `CHANGELOG.md` under Unreleased if user-facing)
- [ ] Zero-dep core preserved (no new required dependencies)
- [ ] Conventional Commit title (`feat:` / `fix:` / `docs:` / `test:` / `bench:` / `chore:`)
