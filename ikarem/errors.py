"""Framework exceptions + handler registry."""

from __future__ import annotations

from typing import Callable, NoReturn


class HTTPException(Exception):
    status_code: int = 500
    detail: str = "Internal Server Error"

    def __init__(self, detail: str | None = None, status_code: int | None = None):
        if detail is not None:
            self.detail = detail
        if status_code is not None:
            self.status_code = status_code
        super().__init__(self.detail)


class BadRequest(HTTPException):
    status_code = 400
    detail = "Bad Request"


class Unauthorized(HTTPException):
    status_code = 401
    detail = "Unauthorized"


class Forbidden(HTTPException):
    status_code = 403
    detail = "Forbidden"


class NotFound(HTTPException):
    status_code = 404
    detail = "Not Found"


class MethodNotAllowed(HTTPException):
    status_code = 405
    detail = "Method Not Allowed"


class PayloadTooLarge(HTTPException):
    status_code = 413
    detail = "Payload Too Large"


class ServiceUnavailable(HTTPException):
    status_code = 503
    detail = "Service Unavailable"


class InternalError(HTTPException):
    status_code = 500
    detail = "Internal Server Error"


_STATUS_MAP = {
    400: BadRequest,
    401: Unauthorized,
    403: Forbidden,
    404: NotFound,
    405: MethodNotAllowed,
    413: PayloadTooLarge,
    500: InternalError,
    503: ServiceUnavailable,
}


def abort(status: int, detail: str | None = None) -> NoReturn:
    """Flask-style abort: raise the HTTPException for a status code.

    abort(404) / abort(403, "owner only") — handled by the normal
    exception pipeline (custom handlers, middleware headers, JSON shape).
    """
    cls = _STATUS_MAP.get(int(status), HTTPException)
    if cls is HTTPException:
        raise HTTPException(detail or "Error", status_code=int(status))
    raise cls(detail) if detail is not None else cls()


Handler = Callable[[object, Exception], object]


class ExceptionHandlers:
    """Maps exception type -> handler. Most-specific match wins via MRO walk."""

    def __init__(self) -> None:
        self._handlers: dict[type, Handler] = {}

    def add(self, exc_type: type[Exception], handler: Handler) -> None:
        self._handlers[exc_type] = handler

    def find(self, exc: Exception) -> Handler | None:
        for klass in type(exc).__mro__:
            if klass in self._handlers:
                return self._handlers[klass]
        return None
