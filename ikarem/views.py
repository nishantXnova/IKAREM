"""MethodView: Flask-style class-based views with full IKAREM DI.

One class per resource; one method per verb. Methods get the complete
treatment — Depends(), Schema bodies, BackgroundTasks — because dispatch
resolves each method through the compiled handler plan::

    class Items(MethodView):
        async def get(self, req, db=Depends(get_db)):
            return {"items": db.all()}
        async def post(self, req, item: Item):
            ...

    app.route("/items", ItemView.methods())(Items.as_view("items"))
"""

from __future__ import annotations

from typing import Any

_VERBS = ("get", "post", "put", "patch", "delete")


class MethodView:
    @classmethod
    def methods(cls) -> list[str]:
        """HTTP verbs this view implements (for app.route)."""
        return [v.upper() for v in _VERBS if callable(getattr(cls, v, None))]

    @classmethod
    def as_view(cls, name: str | None = None, **initkwargs: Any) -> Any:
        def view(req: Any) -> Any:
            self = cls(**initkwargs) if initkwargs else cls()
            return self.dispatch_request(req)

        view.__name__ = name or cls.__name__
        view.__doc__ = cls.__doc__
        view.__view_class__ = cls  # type: ignore
        return view

    async def dispatch_request(self, req: Any) -> Any:
        from .compiled import resolve_compiled
        from .errors import MethodNotAllowed

        handler = getattr(self, req.method.lower(), None)
        if not callable(handler):
            allow = self.methods()
            raise MethodNotAllowed(f"Method {req.method} not allowed. Allow: {allow}")
        # Bound method: `self` already bound, the rest resolves like
        # any handler (Depends, Schema, BackgroundTasks included).
        return await resolve_compiled(handler, req)
