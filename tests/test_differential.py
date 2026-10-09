"""Differential proof: our stdlib implementations agree with the enemy's.

pydantic + PyJWT are TEST-ONLY dependencies (CI installs them in the
differential job; never runtime deps). If these tests pass, the wire
behavior matches — same verdicts, same values, both token directions.
Error *messages* differ by design; verdicts must not.
"""

import pytest

from ikarem import Field as IField
from ikarem import Schema, create_token, verify_token

pydantic = pytest.importorskip("pydantic")
jwt = pytest.importorskip("jwt")


class IItem(Schema):
    name: str = IField(..., min_length=1, max_length=80)
    qty: int = IField(1, ge=1, le=99)
    price: float = 0.0
    active: bool = False
    tags: list = []


class PItem(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra="ignore")

    name: str = pydantic.Field(min_length=1, max_length=80)
    qty: int = pydantic.Field(default=1, ge=1, le=99)
    price: float = 0.0
    active: bool = False
    tags: list = []


def _verdict(fn, data):
    try:
        return ("ok", fn(data))
    except Exception:
        return ("err", None)


CASES = [
    {"name": "apple", "qty": "2", "price": 1.5, "active": "true", "tags": [1, "2"]},
    {"name": "x"},
    {"name": ""},
    {"qty": 0},
    {"name": "x", "qty": 100},
    {"name": "x", "bogus": 1},
    {"name": "x", "qty": "many"},
    {"name": "x", "active": "yes"},
]


def test_validation_verdicts_agree():
    for body in CASES:
        mine, theirs = _verdict(IItem.validate, body), _verdict(PItem.model_validate, body)
        assert mine[0] == theirs[0], f"verdict split on {body}: ikarem={mine[0]} pydantic={theirs[0]}"


def test_validation_values_agree():
    body = {"name": "apple", "qty": "2", "price": 1, "active": "true", "tags": [1, "2"]}
    m, t = IItem.validate(body), PItem.model_validate(body)
    assert (m.name, m.qty, m.price, m.active) == (t.name, t.qty, t.price, t.active)
    assert [int(x) for x in m.tags] == [int(x) for x in t.tags]


def test_pydantic_model_as_handler_body():
    # The seam: a pydantic model where a Schema goes — validation,
    # 400s, OpenAPI, and MCP all treat it as a body model. No import
    # cost when pydantic is absent (duck-typed, never imported by core).
    from ikarem import Ikarem
    from ikarem.testing import TestClient

    class POrder(pydantic.BaseModel):
        item: str
        qty: int = 1

    app = Ikarem(enable_docs=False)

    @app.post("/orders")
    async def create(order: POrder):
        return {"item": order.item, "qty": order.qty}

    c = TestClient(app)
    assert c.post("/orders", body={"item": "apple", "qty": "2"}).json() == {
        "item": "apple",
        "qty": 2,
    }
    assert c.post("/orders", body={"qty": 1}).status_code == 400
    assert app.check()["errors"] == []
    from ikarem.openapi import build_openapi

    body = build_openapi(app)["paths"]["/orders"]["post"]["requestBody"]["content"]["application/json"][
        "schema"
    ]
    assert body["properties"]["item"] == {"title": "Item", "type": "string"}
    tools = {t["name"]: t for t in app.mcp_tools()}
    assert tools["create"]["inputSchema"]["properties"]["qty"]["default"] == 1


def test_tokens_verify_both_directions():
    secret = "differential-secret-at-least-32-bytes!!"
    # ours -> theirs
    mine = create_token("u1", secret, expires_in=900, roles=["admin"])
    claims = jwt.decode(mine, secret, algorithms=["HS256"])
    assert claims["sub"] == "u1" and claims["roles"] == ["admin"]
    # theirs -> ours
    import time as _time

    theirs = jwt.encode({"sub": "u2", "exp": int(_time.time()) + 900}, secret, algorithm="HS256")
    back = verify_token(theirs, secret)
    assert back["sub"] == "u2"
    # tampered in transit fails on both sides (flip the FIRST signature char:
    # fully significant bits — flipping the last char is a proven flake,
    # its low bits are base64 padding and can decode to identical bytes)
    _h, _p, _s = mine.split(".")
    bad = ".".join([_h, _p, ("A" if not _s.startswith("A") else "B") + _s[1:]])
    with pytest.raises(Exception):
        jwt.decode(bad, secret, algorithms=["HS256"])
    import ikarem.auth as _auth

    with pytest.raises(ValueError):
        _auth.verify_token(bad, secret)
