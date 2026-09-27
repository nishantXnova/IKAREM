"""IKAREM public API."""

from .app import Ikarem
from .auth import BearerAuth, check_password, create_token, hash_password, require_roles, verify_token
from .background import BackgroundTasks
from .cache import CacheBackend, MemoryCache, cached
from .config import Config
from .di import Depends
from .errors import (
    BadRequest,
    Forbidden,
    HTTPException,
    InternalError,
    MethodNotAllowed,
    NotFound,
    PayloadTooLarge,
    Unauthorized,
)
from .http import (
    FormData,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Request,
    Response,
    StreamingResponse,
    TextResponse,
    UploadFile,
)
from .mcp import MCPServer
from .middleware import Middleware, MiddlewareStack
from .observability import MetricsMiddleware, RequestIDMiddleware, configure_logging
from .plugins import BasePlugin, Plugin, PluginManager
from .routing import Router
from .security import CORSMiddleware, RateLimitMiddleware, SecurityHeadersMiddleware
from .session import CSRFMiddleware, SessionMiddleware, csrf_token
from .static import FileResponse
from .validation import Field, FieldInfo, Schema, ValidationError
from .websocket import WebSocket

__all__ = [
    "Ikarem",
    "Request",
    "Response",
    "JSONResponse",
    "TextResponse",
    "HTMLResponse",
    "RedirectResponse",
    "StreamingResponse",
    "FileResponse",
    "Router",
    "Middleware",
    "MiddlewareStack",
    "Plugin",
    "PluginManager",
    "BasePlugin",
    "Config",
    "HTTPException",
    "NotFound",
    "MethodNotAllowed",
    "BadRequest",
    "Unauthorized",
    "Forbidden",
    "InternalError",
    "PayloadTooLarge",
    "Schema",
    "ValidationError",
    "Field",
    "FieldInfo",
    "Depends",
    "BackgroundTasks",
    "create_token",
    "verify_token",
    "hash_password",
    "check_password",
    "BearerAuth",
    "require_roles",
    "SessionMiddleware",
    "CSRFMiddleware",
    "csrf_token",
    "FormData",
    "UploadFile",
    "CORSMiddleware",
    "SecurityHeadersMiddleware",
    "RateLimitMiddleware",
    "CacheBackend",
    "MemoryCache",
    "cached",
    "WebSocket",
    "RequestIDMiddleware",
    "MetricsMiddleware",
    "configure_logging",
    "MCPServer",
]

__version__ = "0.3.0"
