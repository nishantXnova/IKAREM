"""Field() constraints: ranges, lengths, patterns, emails, schemas."""

import pytest

from ikarem import Field, Schema, ValidationError


class User(Schema):
    name: str = Field(..., min_length=1, max_length=30, description="display name")
    age: int = Field(0, ge=0, le=150)
    email: str = Field(..., email=True)
    code: str = Field("X-1", pattern=r"^X-\d+$")
    score: float = Field(0.0, gt=0.0, lt=100.0)


def test_valid_coerced_and_defaults():
    u = User.validate({"name": "amy", "email": "a@b.co", "age": "3"})
    assert (u.name, u.age, u.code, u.score) == ("amy", 3, "X-1", 0.0)


def test_required_field_missing():
    with pytest.raises(ValidationError):
        User.validate({"email": "a@b.co"})


def test_length_violations():
    with pytest.raises(ValidationError):
        User.validate({"name": "", "email": "a@b.co"})
    with pytest.raises(ValidationError):
        User.validate({"name": "x" * 31, "email": "a@b.co"})


def test_range_violations():
    with pytest.raises(ValidationError):
        User.validate({"name": "amy", "email": "a@b.co", "age": 200})
    with pytest.raises(ValidationError):
        User.validate({"name": "amy", "email": "a@b.co", "age": -1})
    with pytest.raises(ValidationError):
        User.validate({"name": "amy", "email": "a@b.co", "score": 0.0})


def test_pattern_and_email():
    with pytest.raises(ValidationError):
        User.validate({"name": "amy", "email": "not-an-email"})
    with pytest.raises(ValidationError):
        User.validate({"name": "amy", "email": "a@b.co", "code": "nope"})


def test_json_schema_carries_constraints():
    js = User.json_schema()
    assert js["properties"]["age"] == {"type": "integer", "minimum": 0, "maximum": 150}
    assert js["properties"]["email"] == {"type": "string", "format": "email"}
    assert js["properties"]["code"]["pattern"] == r"^X-\d+$"
    assert js["properties"]["name"]["description"] == "display name"
    assert js["properties"]["name"]["maxLength"] == 30
    assert set(js["required"]) == {"name", "email"}


def test_plain_fields_untouched():
    class M(Schema):
        a: str
        b: int = 2

    assert M.validate({"a": "x"}).dict() == {"a": "x", "b": 2}
    assert M.json_schema()["properties"]["a"] == {"type": "string"}


def test_list_length_constraints():
    class T(Schema):
        tags: list = Field(default=[], min_length=1, max_length=3)

    assert T.validate({"tags": ["a"]}).tags == ["a"]
    with pytest.raises(ValidationError):
        T.validate({"tags": []})
    with pytest.raises(ValidationError):
        T.validate({"tags": ["a", "b", "c", "d"]})
