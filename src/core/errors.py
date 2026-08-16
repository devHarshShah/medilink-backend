"""Error handlers.

The frontend's axios interceptor (medilink-frontend/src/lib/api.ts) reads
`error.response.data.detail` and accepts either a string or FastAPI's
[{loc, msg, type}] array. Anything else surfaces as an unhelpful toast, so every
error leaving this app must be shaped as {"detail": ...}.
"""

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger(__name__)


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(request: Request, exc: StarletteHTTPException):
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.detail},
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(request: Request, exc: RequestValidationError):
        # Keep FastAPI's native [{loc, msg, type}] shape - the frontend parses it.
        details = []
        for err in exc.errors():
            details.append(
                {
                    "loc": [str(part) for part in err.get("loc", [])],
                    "msg": err.get("msg", "Invalid input"),
                    "type": err.get("type", "value_error"),
                }
            )
        return JSONResponse(status_code=422, content={"detail": details})

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception):
        logger.exception("Unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse(
            status_code=500,
            content={"detail": "An unexpected error occurred. Please try again."},
        )
