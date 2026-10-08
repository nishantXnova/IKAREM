"""IKAREM public API."""

from .app import Ikarem
from .auth import (
    APIKeyAuth,
    BearerAuth,
    check_password,
    create_token,
    hash_password,
    require_if,
    require_roles,
    require_scopes,
    verify_token,
)
from .background import BackgroundTasks
from .blueprints import Blueprint
from .cache import CacheBackend, MemoryCache, RedisCache, cached
from .conditional import ConditionalMiddleware
from .config import Config
from .deprecation import deprecated
from .di import Depends
from .errors import (
    BadRequest,
    Forbidden,
    HTTPException,
    InternalError,
    MethodNotAllowed,
    NotFound,
    PayloadTooLarge,
    ServiceUnavailable,
    Unauthorized,
    abort,
)
from .flashing import flash, get_flashed_messages
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
    XMLResponse,
    dict_to_xml,
    escape_html,
)
from .mcp import MCPServer
from .middleware import Middleware, MiddlewareStack
from .migrations import Migrator
from .nish import NISHResponse, from_nish, negotiate, to_nish
from .nitro import nitro
from .observability import MetricsMiddleware, RequestIDMiddleware, configure_logging
from .plugins import BasePlugin, Plugin, PluginManager
from .queue import Queue, QueuePlugin, run_worker, task
from .resilience import ConcurrencyLimitMiddleware, IdempotencyMiddleware, SpikeManager, TimeoutMiddleware
from .resources import resource
from .routing import Router
from .scheduler import Scheduler, SchedulerPlugin, parse_cron, run_scheduler
from .security import (
    CORSMiddleware,
    RateLimitMiddleware,
    RedisRateLimitMiddleware,
    SecurityHeadersMiddleware,
    TrustedHostMiddleware,
)
from .security_audit import audit_report
from .session import CSRFMiddleware, SessionMiddleware, csrf_token
from .static import FileResponse
from .templating import Templates
from .tracing import TracingMiddleware
from .validation import Field, FieldInfo, Schema, ValidationError
from .views import MethodView
from .websocket import Room, WebSocket, WebSocketDisconnect

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
    "abort",
    "NotFound",
    "MethodNotAllowed",
    "BadRequest",
    "Unauthorized",
    "Forbidden",
    "InternalError",
    "ServiceUnavailable",
    "PayloadTooLarge",
    "Blueprint",
    "MethodView",
    "Templates",
    "flash",
    "get_flashed_messages",
    "Migrator",
    "Queue",
    "QueuePlugin",
    "run_worker",
    "task",
    "Scheduler",
    "SchedulerPlugin",
    "parse_cron",
    "run_scheduler",
    "resource",
    "deprecated",
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
    "require_scopes",
    "require_if",
    "APIKeyAuth",
    "SessionMiddleware",
    "CSRFMiddleware",
    "csrf_token",
    "FormData",
    "UploadFile",
    "XMLResponse",
    "dict_to_xml",
    "escape_html",
    "CORSMiddleware",
    "SecurityHeadersMiddleware",
    "TrustedHostMiddleware",
    "RateLimitMiddleware",
    "RedisRateLimitMiddleware",
    "audit_report",
    "TracingMiddleware",
    "TimeoutMiddleware",
    "ConcurrencyLimitMiddleware",
    "SpikeManager",
    "IdempotencyMiddleware",
    "CacheBackend",
    "MemoryCache",
    "RedisCache",
    "cached",
    "nitro",
    "ConditionalMiddleware",
    "Room",
    "WebSocket",
    "WebSocketDisconnect",
    "RequestIDMiddleware",
    "MetricsMiddleware",
    "configure_logging",
    "MCPServer",
    "NISHResponse",
    "from_nish",
    "negotiate",
    "to_nish",
]

__version__ = "1.3.0"
