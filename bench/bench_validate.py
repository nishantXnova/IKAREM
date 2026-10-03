"""Single-body validation: ours vs pydantic. The honest benchmark.

Batch-validation shootouts (100k records) always favor Rust and never
matter: web frameworks validate ONE body per request. This measures
that — microseconds per body, same shape, same coercions.

Needs pydantic installed (test/bench env only, never a runtime dep):
    pip install pydantic
Without it, prints IKAREM-only numbers and exits 0.
"""

import sys
import timeit

sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))

from ikarem import Field, Schema  # noqa: E402

BODY = {"name": "apple", "qty": "2", "price": 1.5, "active": "true"}
BAD = {"qty": 0}


class Item(Schema):
    name: str = Field(..., min_length=1, max_length=80)
    qty: int = Field(1, ge=1, le=99)
    price: float = 0.0
    active: bool = False


def ours_valid():
    Item.validate(BODY)


def ours_invalid():
    try:
        Item.validate(BAD)
    except Exception:
        pass


def bench(fn, n=20000):
    fn()  # warmup
    dt = timeit.timeit(fn, number=n)
    return dt / n * 1e6


ours_us = bench(ours_valid)
ours_bad_us = bench(ours_invalid)
print(f"ikarem Schema.validate (valid):   {ours_us:8.2f} us/body")
print(f"ikarem Schema.validate (invalid): {ours_bad_us:8.2f} us/body")

try:
    import pydantic
except ImportError:
    print("pydantic not installed — IKAREM-only rows (pip install pydantic for the duel)")
    raise SystemExit(0)


class PItem(pydantic.BaseModel):
    name: str = pydantic.Field(min_length=1, max_length=80)
    qty: int = pydantic.Field(default=1, ge=1, le=99)
    price: float = 0.0
    active: bool = False


def theirs_valid():
    PItem.model_validate(BODY)


def theirs_invalid():
    try:
        PItem.model_validate(BAD)
    except Exception:
        pass


their_us = bench(theirs_valid)
their_bad_us = bench(theirs_invalid)
print(f"pydantic model_validate (valid):  {their_us:8.2f} us/body")
print(f"pydantic model_validate (invalid):{their_bad_us:8.2f} us/body")
print(f"ratio (valid): {ours_us / their_us:.2f}x — per-request cost, not batch lore")
print(f"pydantic {pydantic.VERSION}")
