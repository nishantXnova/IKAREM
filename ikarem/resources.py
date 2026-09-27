"""app.resource(): a validated, paginated CRUD collection in one call.

    app.resource("/items", ItemIn, table="items", owner_field="user_id")

Generates, all validated through ``Schema`` so OpenAPI documents them::

    GET    /items            list (q search, page)
    POST   /items            create -> 201
    GET    /items/{id:int}   retrieve (404 when missing/foreign)
    PUT    /items/{id:int}   full update
    DELETE /items/{id:int}   delete

Conventions (SQLite + Postgres both): table has INTEGER/SERIAL ``id`` PK;
``db`` defaults to ``request.app.state_db`` (DatabasePlugin). When
``owner_field`` is set, every row is scoped to ``owner(req)`` (default:
session ``uid``) — anonymous callers get 401, never someone else's rows.
"""

# NOTE: no `from __future__ import annotations` here — the generated
# handlers annotate Schema params with a closure variable, which must
# evaluate eagerly at def time (strings can't resolve closure scope).
from typing import Any, Callable


def resource(
    app: Any,
    prefix: str,
    schema: Any,
    table: str,
    *,
    db_getter: Callable[[Any], Any] | None = None,
    owner_field: str | None = None,
    owner: Callable[[Any], Any] | None = None,
    id_field: str = "id",
    page_size: int = 20,
    search_fields: tuple[str, ...] = ("description",),
    name: str | None = None,
) -> Any:
    from .di import Depends
    from .errors import NotFound, Unauthorized
    from .validation import Schema

    if not (isinstance(schema, type) and issubclass(schema, Schema)):
        raise TypeError("resource() schema must be a Schema subclass")
    prefix = "/" + prefix.strip("/")
    tag = name or table

    def _db(req: Any) -> Any:
        if db_getter is not None:
            return db_getter(req)
        db = getattr(getattr(req, "app", None), "state_db", None)
        if db is None:
            raise RuntimeError(f"resource {prefix!r} needs DatabasePlugin (state_db)")
        return db

    def _owner(req: Any) -> Any:
        if owner is not None:
            return owner(req)
        sess = getattr(req, "session", None)
        return sess.get("uid") if sess else None

    def _scope(uid: Any) -> tuple[str, list]:
        if owner_field:
            if uid is None:
                raise Unauthorized("login required")
            return f" AND {owner_field} = ?", [uid]
        return "", []

    async def _list(req: Any, db=Depends(_db), uid=Depends(_owner)):
        where, args = _scope(uid)
        q = req.query.get("q", "")
        if q and search_fields:
            ors = " OR ".join(f"{c} LIKE ?" for c in search_fields)
            where += f" AND ({ors})"
            args.extend(f"%{q}%" for _ in search_fields)
        page = max(1, int(req.query.get("page", "1") or 1))
        total = (await db.fetch_one(f"SELECT COUNT(*) AS n FROM {table} WHERE 1=1{where}", *args))["n"]
        rows = await db.fetch_all(
            f"SELECT * FROM {table} WHERE 1=1{where} ORDER BY {id_field} DESC LIMIT ? OFFSET ?",
            *args,
            page_size,
            (page - 1) * page_size,
        )
        return {"total": total, "page": page, "items": rows}

    async def _create(req: Any, item: schema, db=Depends(_db), uid=Depends(_owner)):  # type: ignore
        data = item.dict()
        cols = list(data)
        if owner_field:
            # Owner always comes from the session, never the body: a client
            # sending user_id must not create rows as someone else.
            if uid is None:
                raise Unauthorized("login required")
            data[owner_field] = uid
            if owner_field not in cols:
                cols.append(owner_field)
        placeholders = ", ".join(["?"] * len(cols))
        if getattr(db, "dialect", "sqlite") == "postgres":
            row = await db.fetch_one(
                f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({placeholders}) RETURNING {id_field}",
                *[data[c] for c in cols],
            )
            new_id = row[id_field] if row else None
        else:
            new_id = await db.execute(
                f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({placeholders})",
                *[data[c] for c in cols],
            )
        return {"id": new_id, **data}, 201

    async def _one(req: Any, rid: int, db: Any, uid: Any) -> Any:
        where, args = _scope(uid)
        row = await db.fetch_one(f"SELECT * FROM {table} WHERE {id_field} = ?{where}", rid, *args)
        if not row:
            raise NotFound(f"{tag} not found")
        return row

    async def _retrieve(req: Any, rid: int, db=Depends(_db), uid=Depends(_owner)):
        return await _one(req, rid, db, uid)

    async def _update(req: Any, rid: int, item: schema, db=Depends(_db), uid=Depends(_owner)):  # type: ignore
        from .errors import BadRequest

        await _one(req, rid, db, uid)
        data = item.dict()
        if owner_field:
            data.pop(owner_field, None)  # ownership is session-derived, never body-set
        if not data:
            raise BadRequest("nothing to update")
        sets = ", ".join(f"{c} = ?" for c in data)
        where, args = _scope(uid)
        await db.execute(f"UPDATE {table} SET {sets} WHERE {id_field} = ?{where}", *data.values(), rid, *args)
        return {"id": rid, **data}

    async def _delete(req: Any, rid: int, db=Depends(_db), uid=Depends(_owner)):
        await _one(req, rid, db, uid)
        where, args = _scope(uid)
        await db.execute(f"DELETE FROM {table} WHERE {id_field} = ?{where}", rid, *args)
        return {"ok": True}

    app.router.add(prefix, {"GET"}, _list, name=f"{tag}.list")
    app.router.add(prefix, {"POST"}, _create, name=f"{tag}.create")
    app.router.add(prefix + "/{rid:int}", {"GET"}, _retrieve, name=f"{tag}.retrieve")
    app.router.add(prefix + "/{rid:int}", {"PUT"}, _update, name=f"{tag}.update")
    app.router.add(prefix + "/{rid:int}", {"DELETE"}, _delete, name=f"{tag}.delete")
    return tag
