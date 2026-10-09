# Publishing to PyPI (no API tokens)

All three packages publish via **Trusted Publisher (OIDC)** — GitHub proves
identity, PyPI needs no long-lived token. The old account-scoped token (open
thread #1) can be revoked once this is green; nothing uses it.

## One-time setup (PyPI website, 2 minutes per new package)

`ikarem` already has a publisher. For each of `ikarem-pentest`,
`ikarem-oauth` (first release only):

1. https://pypi.org/manage/account/publishing/ → **Add a new pending publisher**
2. PyPI Project Name: `ikarem-pentest` (then repeat for `ikarem-oauth`, `ikarem-backup`)
3. Owner: `nishantXnova`, Repository: `IKAREM`
4. Workflow name: `publish.yml`, Environment name: `pypi`
5. The first tag push creates the project; later pushes reuse it.

## Release runbook

```bash
# core (publishes ikarem X.Y.Z):
git tag v1.4.0 && git push origin v1.4.0
# extensions (independent versions, own tags):
git tag pentest-v0.1.0 && git push origin pentest-v0.1.0
git tag oauth-v0.1.0 && git push origin oauth-v0.1.0
git tag backup-v0.1.0 && git push origin backup-v0.1.0
```

Version rules (enforced by `tests/test_packaging.py` for core):
`pyproject.toml` == `ikarem.__version__` == `CHANGELOG ## [x.y.z]`.
Site stamps (`v…` spans, footers, JSON-LD, llms.txt) ride along.

## Pre-flight (local, before tagging)

```bash
pip install build twine
python -m build && twine check dist/*
cd extensions/ikarem-pentest && python -m build && twine check dist/*
```
