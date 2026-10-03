"""Zero-dep validation: Schema models from type hints (FastAPI/Pydantic-lite).

class CreateUser(Schema):
    name: str
    age: int = 0
    email: str = ""

u = CreateUser.validate({"name": "a", "age": "3"})  # coerces, raises ValidationError
"""

from __future__ import annotations

import inspect
from typing import Any, Union, get_args, get_origin


class ValidationError(Exception):
    def __init__(self, errors: list[dict]):
        self.errors = errors
        super().__init__(str(errors))


_MISSING = object()
_REQUIRED = ...  # Field() with no default means required


class FieldInfo:
    """Per-field constraints: Field(18, ge=0, le=150, description=...)."""

    def __init__(
        self,
        default: Any = _REQUIRED,
        *,
        ge: Any = None,
        le: Any = None,
        gt: Any = None,
        lt: Any = None,
        min_length: int | None = None,
        max_length: int | None = None,
        pattern: str | None = None,
        email: bool = False,
        description: str | None = None,
        title: str | None = None,
    ):
        self.default = default
        self.ge = ge
        self.le = le
        self.gt = gt
        self.lt = lt
        self.min_length = min_length
        self.max_length = max_length
        self.pattern = pattern
        self.email = email
        self.description = description
        self.title = title


def Field(default: Any = _REQUIRED, **constraints: Any) -> FieldInfo:
    """Declare a constrained field: name: str = Field(..., min_length=1)."""
    return FieldInfo(default, **constraints)


_EMAIL_RE = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"


def _check_constraints(value: Any, field: str, c: FieldInfo | None, errors: list) -> bool:
    """Returns False (and records) when a constraint is violated."""
    if c is None:
        return True
    import re as _re

    if isinstance(value, bool):  # bool is not a number for range checks
        numeric, sized = False, False
    else:
        numeric = isinstance(value, (int, float))
        sized = isinstance(value, (str, list, tuple))
    if numeric:
        if c.ge is not None and not value >= c.ge:
            errors.append({"field": field, "error": f"must be >= {c.ge}", "value": value})
            return False
        if c.le is not None and not value <= c.le:
            errors.append({"field": field, "error": f"must be <= {c.le}", "value": value})
            return False
        if c.gt is not None and not value > c.gt:
            errors.append({"field": field, "error": f"must be > {c.gt}", "value": value})
            return False
        if c.lt is not None and not value < c.lt:
            errors.append({"field": field, "error": f"must be < {c.lt}", "value": value})
            return False
    if sized:
        n = len(value)
        if c.min_length is not None and n < c.min_length:
            errors.append({"field": field, "error": f"length must be >= {c.min_length}", "value": value})
            return False
        if c.max_length is not None and n > c.max_length:
            errors.append({"field": field, "error": f"length must be <= {c.max_length}", "value": value})
            return False
    if isinstance(value, str):
        if c.pattern is not None and not _re.search(c.pattern, value):
            errors.append({"field": field, "error": f"must match {c.pattern!r}", "value": value})
            return False
        if c.email and not _re.match(_EMAIL_RE, value):
            errors.append({"field": field, "error": "must be a valid email", "value": value})
            return False
    return True


def _coerce(value: Any, ann: Any, field: str, errors: list, constraints: FieldInfo | None = None) -> Any:
    if value is None:
        return None
    origin = get_origin(ann)
    args = get_args(ann)
    # Optional[X]
    if origin is Union and type(None) in args:
        non_none = [a for a in args if a is not type(None)]
        if value is None:
            return None
        return _coerce(value, non_none[0], field, errors, constraints)
    try:
        if ann is Any or ann is inspect._empty:
            coerced = value
        elif ann is str:
            coerced = str(value)
        elif ann is int:
            if isinstance(value, bool):
                raise ValueError("bool is not int")
            coerced = int(value)
        elif ann is float:
            coerced = float(value)
        elif ann is bool:
            if isinstance(value, bool):
                coerced = value
            elif isinstance(value, str):
                lv = value.lower()
                if lv in ("true", "1", "yes", "on"):
                    coerced = True
                elif lv in ("false", "0", "no", "off"):
                    coerced = False
                else:
                    raise ValueError(f"invalid bool {value!r}")
            else:
                coerced = bool(value)
        elif origin in (list, tuple):
            (inner,) = args or (Any,)
            if not isinstance(value, (list, tuple)):
                raise ValueError("expected list")
            coerced = [_coerce(v, inner, field, errors) for v in value]
        elif origin is dict or ann is dict:
            if not isinstance(value, dict):
                raise ValueError("expected object")
            coerced = dict(value)
        elif inspect.isclass(ann) and issubclass(ann, Schema):
            # Nested Schema
            if isinstance(value, ann):
                return value
            coerced = ann(**value) if isinstance(value, dict) else ann(value)
        else:
            coerced = value
        if not _check_constraints(coerced, field, constraints, errors):
            return _MISSING
        return coerced
    except ValidationError:
        raise
    except Exception as e:
        errors.append({"field": field, "error": str(e), "value": value})
        return _MISSING


class Schema:
    _extra = "ignore"

    def __init_subclass__(cls, extra: str = "ignore", **kw: Any) -> None:
        super().__init_subclass__(**kw)
        if extra not in ("ignore", "forbid"):
            raise TypeError("Schema extra must be 'ignore' or 'forbid'")
        cls._extra = extra

    def __init__(self, *args: Any, **kwargs: Any):
        hints = self.__class__.__hints__()
        constraints = self.__class__.__constraints__()
        data = dict(kwargs)
        if args and len(args) == 1 and isinstance(args[0], dict):
            data = {**args[0], **kwargs}
        errors: list[dict] = []
        for name, ann in hints.items():
            raw_default = getattr(self.__class__, name, _MISSING)
            field_info = raw_default if isinstance(raw_default, FieldInfo) else None
            default = field_info.default if field_info else raw_default
            has_default = default is not _MISSING and default is not _REQUIRED
            if name not in data:
                if has_default:
                    setattr(self, name, default)
                    continue
                # Optional without default -> None allowed?
                origin, oargs = get_origin(ann), get_args(ann)
                if origin is Union and type(None) in oargs:
                    setattr(self, name, None)
                    continue
                errors.append({"field": name, "error": "required", "value": None})
                continue
            v = _coerce(data[name], ann, name, errors, constraints.get(name))
            if v is not _MISSING:
                setattr(self, name, v)
        if self.__class__._extra == "forbid":
            for key in data:
                if key not in hints:
                    errors.append({"field": key, "error": "unexpected", "value": data[key]})
        if errors:
            raise ValidationError(errors)

    @classmethod
    def __hints__(cls) -> dict:
        out: dict = {}
        for klass in reversed(cls.__mro__):
            out.update(getattr(klass, "__annotations__", {}))
        return out

    @classmethod
    def __constraints__(cls) -> dict:
        out: dict = {}
        for klass in reversed(cls.__mro__):
            for name, val in getattr(klass, "__dict__", {}).items():
                if isinstance(val, FieldInfo):
                    out[name] = val
        return out

    @classmethod
    def validate(cls, data: Any) -> "Schema":
        if isinstance(data, cls):
            return data
        if not isinstance(data, dict):
            raise ValidationError([{"field": "", "error": "expected object", "value": data}])
        return cls(**data)

    def dict(self) -> dict:
        return {k: getattr(self, k) for k in self.__class__.__hints__() if hasattr(self, k)}

    @classmethod
    def json_schema(cls) -> dict:
        props, required = {}, []
        constraints = cls.__constraints__()
        for name, ann in cls.__hints__().items():
            props[name] = _js_type(ann, constraints.get(name))
            raw = getattr(cls, name, _MISSING)
            default = raw.default if isinstance(raw, FieldInfo) else raw
            if default is _MISSING or default is _REQUIRED:
                # check Optional
                if not (get_origin(ann) is Union and type(None) in get_args(ann)):
                    required.append(name)
        return {"type": "object", "properties": props, "required": required}


def is_schema_like(ann: Any) -> bool:
    """Schema subclasses AND pydantic-style models (``model_validate`` +
    ``model_json_schema``). Duck-typed — pydantic is never imported, so
    the core stays stdlib-only whether or not it is installed."""
    if isinstance(ann, type) and issubclass(ann, Schema):
        return True
    return (
        isinstance(ann, type)
        and callable(getattr(ann, "model_validate", None))
        and callable(getattr(ann, "model_json_schema", None))
    )


def validate_schema(ann: Any, data: Any) -> Any:
    """Validate against a Schema or a pydantic-style model."""
    if isinstance(ann, type) and issubclass(ann, Schema):
        return ann.validate(data)
    return ann.model_validate(data)


def schema_json_schema(ann: Any) -> dict:
    """JSON Schema for a Schema or a pydantic-style model."""
    if isinstance(ann, type) and issubclass(ann, Schema):
        return ann.json_schema()
    schema = ann.model_json_schema()
    return schema if isinstance(schema, dict) else {"type": "object"}


def _js_type(ann: Any, constraints: FieldInfo | None = None) -> dict:
    origin, args = get_origin(ann), get_args(ann)
    if ann is str:
        t: dict = {"type": "string"}
    elif ann is int:
        t = {"type": "integer"}
    elif ann is float:
        t = {"type": "number"}
    elif ann is bool:
        t = {"type": "boolean"}
    elif origin in (list, tuple):
        t = {"type": "array", "items": _js_type(args[0]) if args else {}}
    elif origin is Union and type(None) in args:
        inner = [a for a in args if a is not type(None)][0]
        t = _js_type(inner, constraints)
        t["nullable"] = True
        return t
    elif inspect.isclass(ann) and issubclass(ann, Schema):
        return ann.json_schema()
    else:
        return {}
    if constraints is not None:
        c = constraints
        if c.min_length is not None and t.get("type") in ("string", "array"):
            t["minLength" if t["type"] == "string" else "minItems"] = c.min_length
        if c.max_length is not None and t.get("type") in ("string", "array"):
            t["maxLength" if t["type"] == "string" else "maxItems"] = c.max_length
        if c.ge is not None:
            t["minimum"] = c.ge
        if c.le is not None:
            t["maximum"] = c.le
        if c.gt is not None:
            t["exclusiveMinimum"] = c.gt
        if c.lt is not None:
            t["exclusiveMaximum"] = c.lt
        if c.pattern is not None and t.get("type") == "string":
            t["pattern"] = c.pattern
        if c.email and t.get("type") == "string":
            t["format"] = "email"
        if c.description is not None:
            t["description"] = c.description
        if c.title is not None:
            t["title"] = c.title
    return t
