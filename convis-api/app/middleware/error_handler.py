"""
Global error handler for standardized error responses
Critical for production error handling
"""
from fastapi import Request, status
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError, HTTPException
from starlette.exceptions import HTTPException as StarletteHTTPException
import logging
import traceback

logger = logging.getLogger(__name__)


def _sanitize_validation_errors(errors):
    """Pydantic v2's `exc.errors()` includes the raw input under 'input',
    which may be bytes (when the client posts a non-JSON body). bytes can't
    be JSON-encoded → the handler itself raises and surfaces as a 500.

    We strip / coerce non-serialisable values so the error response is
    always valid JSON, regardless of what garbage the client sent.
    """
    safe = []
    for err in errors:
        copy = dict(err)
        inp = copy.get("input")
        if isinstance(inp, (bytes, bytearray)):
            try:
                copy["input"] = inp.decode("utf-8", errors="replace")[:200]
            except Exception:
                copy["input"] = f"<{len(inp)} bytes>"
        elif inp is not None and not isinstance(inp, (str, int, float, bool, list, dict, type(None))):
            copy["input"] = repr(inp)[:200]
        # `ctx` can also contain non-serialisable error objects in v2
        ctx = copy.get("ctx")
        if isinstance(ctx, dict):
            copy["ctx"] = {k: (str(v) if not isinstance(v, (str, int, float, bool, type(None))) else v)
                           for k, v in ctx.items()}
        safe.append(copy)
    return safe


async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """Handle validation errors with consistent format. Sanitises error
    payloads so non-serialisable inputs (bytes, exception objects) don't
    cause the handler itself to raise."""
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={
            "error": "validation_error",
            "message": "Request validation failed",
            "details": _sanitize_validation_errors(exc.errors()),
            "request_id": getattr(request.state, "request_id", None)
        }
    )


async def http_exception_handler(request: Request, exc: HTTPException):
    """Handle HTTP exceptions with consistent format"""
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": exc.detail if isinstance(exc.detail, str) else "http_error",
            "message": exc.detail if isinstance(exc.detail, str) else str(exc.detail),
            "status_code": exc.status_code,
            "request_id": getattr(request.state, "request_id", None)
        }
    )


async def starlette_exception_handler(request: Request, exc: StarletteHTTPException):
    """Handle Starlette HTTP exceptions"""
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": "http_error",
            "message": exc.detail,
            "status_code": exc.status_code,
            "request_id": getattr(request.state, "request_id", None)
        }
    )


async def general_exception_handler(request: Request, exc: Exception):
    """Handle unexpected exceptions"""
    request_id = getattr(request.state, "request_id", None)
    
    # Log full traceback
    logger.error(
        f"Unhandled exception in request {request_id}: {str(exc)}",
        exc_info=True,
        extra={"request_id": request_id, "path": request.url.path}
    )
    
    # Don't expose internal errors in production
    is_production = getattr(request.app.state, "environment", "development") == "production"
    
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "error": "internal_server_error",
            "message": "An unexpected error occurred" if is_production else str(exc),
            "request_id": request_id
        }
    )

