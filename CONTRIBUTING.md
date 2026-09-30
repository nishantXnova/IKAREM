# Contributing to IKAREM

Thanks for building with us. This doc is the whole process — follow it and
your PR will merge fast.

## Setup (minutes)

```bash
git clone https://github.com/nishantXnova/IKAREM
cd IKAREM
python -m venv .venv && .venv/Scripts/activate   # Windows; source .venv/bin/activate elsewhere
pip install -e ".[dev]"
python -m pytest tests/ ledger/tests cadence/tests -q
```

## What to run before every PR

```bash
ruff check . && ruff format --check .
python -m pytest tests/ ledger/tests cadence/tests -q
python -m ikarem.cli check ledger.app:app
python bench/bench_switch.py   # only if you touched the hot path
```

## Standards

- **Zero-dep core is sacred.** `ikarem/` must import from stdlib only.
  Optional integrations live behind lazy imports + extras in `pyproject.toml`.
- **Every behavior ships with a test** in `tests/` (framework),
  `ledger/tests/` or `cadence/tests/` (showcase apps). Bugfix PRs include a
  regression test.
- **Docs move with code.** User-facing change → update `README.md` and
  (for switches/upgrades) `docs/MIGRATING_FROM_MERAKI.md`.
- **Keep the flame graphs flat.** No per-request `inspect.signature`,
  no reflection in `ikarem/compiled.py`'s resolve path, no regex where a
  dict works. Prove it with the bench, not adjectives.

## Branches & commits

- Branch from `main`: `feat/<slug>`, `fix/<slug>`, `docs/<slug>`, `bench/<slug>`.
- [Conventional Commits](https://www.conventionalcommits.org/): `feat:`, `fix:`,
  `docs:`, `test:`, `bench:`, `chore:`. Releases and the changelog are cut
  from these messages — get the prefix right.

## Pull requests

1. Fill in `.github/PULL_REQUEST_TEMPLATE.md` (it enforces the checklist).
2. One concern per PR. Small diffs beat manifestos.
3. CI must be green on all OS/Python combos — don't merge red.
4. A maintainer reviews for API surface: new public names need a test,
   a doc line, and an `__all__` entry where applicable.

## Reporting bugs / requesting features

Use the issue templates (bug report / feature request). A bug report without
a minimal reproducer will be asked for one before triage.
