"""``python -m ikarem_haproxy`` -> the same CLI as the console script."""

from .cli import main

if __name__ == "__main__":  # pragma: no cover - trivial entry point
    raise SystemExit(main())
