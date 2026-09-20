"""FastAPI application for the local Jev gateway."""
from __future__ import annotations

import asyncio
import hmac
import json
import os
import re
import sys
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .config import Settings
from .schema import SystemOneRequest, SystemOneResponse
from .service import Gateway, RequestError

_SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_RELEASE_DATE = "2026-09-20"


def _error(status: int, message: str, kind: str = "request_error") -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"error": {"message": message, "type": kind}},
    )


def _request_id(headers: Headers) -> str:
    candidate = headers.get("x-typesafe-request-id", "")
    if _SAFE_REQUEST_ID.fullmatch(candidate):
        return candidate
    return uuid.uuid4().hex


def _log_event(event: dict[str, Any]) -> None:
    print(json.dumps(event, ensure_ascii=False, separators=(",", ":")), file=sys.stderr, flush=True)


def _failure_event(request: Request, status: int) -> dict[str, Any]:
    return {
        "event": "evaluation.failed",
        "request_id": request.state.request_id,
        "config_id": request.app.state.settings.config_id,
        "duration_ms": round((time.perf_counter() - request.state.started) * 1000, 3),
        "status": status,
    }


class _RequestEnvelopeMiddleware:
    """Enforce the byte limit without obscuring later disconnect messages."""

    def __init__(
        self, app: ASGIApp, *, settings: Settings, downstream_env: str | None
    ) -> None:
        self.app = app
        self.settings = settings
        self.downstream_env = downstream_env

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        request_id = _request_id(headers)
        scope.setdefault("state", {})["request_id"] = request_id
        scope["state"]["started"] = time.perf_counter()

        async def send_with_ids(message: Message) -> None:
            if message["type"] == "http.response.start":
                response_headers = MutableHeaders(scope=message)
                response_headers["x-typesafe-request-id"] = request_id
                response_headers["x-jev-config-id"] = self.settings.config_id
            await send(message)

        chunks: list[bytes] = []
        total = 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            if message["type"] != "http.request":
                continue
            chunk = message.get("body", b"")
            total += len(chunk)
            if total > self.settings.server.max_body_bytes:
                await _error(413, "Request body is too large")(scope, receive, send_with_ids)
                return
            chunks.append(chunk)
            if not message.get("more_body", False):
                break
        body = b"".join(chunks)

        if scope["path"].startswith("/v1/") and self.downstream_env:
            expected = os.environ.get(self.downstream_env)
            supplied = headers.get("authorization", "")
            supplied_token = supplied[7:] if supplied.startswith("Bearer ") else ""
            valid = (
                bool(expected)
                and bool(supplied_token)
                and hmac.compare_digest(
                    supplied_token.encode("utf-8"), expected.encode("utf-8")
                )
            )
            if not valid:
                response = _error(401, "Invalid or missing bearer token", "authentication_error")
                response.headers["www-authenticate"] = "Bearer"
                await response(scope, receive, send_with_ids)
                return

        replayed = False

        async def replay_body() -> Message:
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        await self.app(scope, replay_body, send_with_ids)


def create_app(
    settings: Settings, *, client: httpx.AsyncClient | None = None
) -> FastAPI:
    """Create an application whose lifespan owns only clients it constructs."""
    downstream_env = settings.server.api_key_env
    if downstream_env and not os.environ.get(downstream_env):
        raise ValueError("Configured downstream API key environment variable is missing")

    gateway = Gateway(settings, client)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await gateway.startup()
        try:
            yield
        finally:
            await gateway.aclose()

    app = FastAPI(title="Jev Gateway", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.gateway = gateway
    app.add_middleware(
        _RequestEnvelopeMiddleware,
        settings=settings,
        downstream_env=downstream_env,
    )

    @app.exception_handler(RequestError)
    async def request_error_handler(request: Request, exc: RequestError) -> JSONResponse:
        if request.url.path == "/v1/systemone":
            _log_event(_failure_event(request, exc.status))
        response = _error(exc.status, exc.message)
        if exc.status in {429, 503, 529}:
            response.headers["retry-after"] = "1"
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        details = [
            {
                "loc": list(error.get("loc", ())),
                "msg": str(error.get("msg", "Invalid value")),
                "type": str(error.get("type", "validation_error")),
            }
            for error in exc.errors()
        ]
        if request.url.path == "/v1/systemone":
            _log_event(_failure_event(request, 422))
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "message": "Request validation failed",
                    "type": "validation_error",
                    "details": details,
                }
            },
        )

    @app.get("/")
    async def root() -> dict[str, Any]:
        return {"name": "jev-gateway", "docs": "/docs", "configID": settings.config_id}

    @app.get("/health/live")
    async def live() -> dict[str, str]:
        return {"status": "ok", "scope": "process"}

    @app.get("/health")
    async def health() -> JSONResponse:
        upstream_ready = bool(os.environ.get(settings.upstream.api_key_env))
        downstream_ready = not downstream_env or bool(os.environ.get(downstream_env))
        ready = upstream_ready and downstream_ready
        return JSONResponse(
            status_code=200 if ready else 503,
            content={
                "status": "ready" if ready else "not_ready",
                "scope": "configuration",
                "configID": settings.config_id,
            },
        )

    @app.get("/v1/models")
    async def models() -> dict[str, Any]:
        entries = [
            {
                "name": "jev-latest",
                "description": "Stable alias for the configured Jev decision adapter",
                "release_date": _RELEASE_DATE,
            }
        ]
        if settings.upstream.model != "jev-latest":
            entries.append(
                {
                    "name": settings.upstream.model,
                    "description": "Configured upstream model accepted by this gateway",
                    "release_date": _RELEASE_DATE,
                }
            )
        return {
            "models": entries,
            "object": "list",
            "data": [{"id": entry["name"], "object": "model"} for entry in entries],
        }

    @app.get("/v1/limits")
    async def limits() -> dict[str, int]:
        return {
            "max_answers_per_question": settings.max_answers,
            "max_questions": settings.server.max_questions,
            "max_body_bytes": settings.server.max_body_bytes,
            "max_calls_per_request": settings.server.max_calls_per_request,
            "max_concurrent_requests": settings.server.max_concurrent_requests,
            "max_concurrent_calls": settings.server.max_concurrent_calls,
        }

    @app.post("/v1/systemone", response_model=SystemOneResponse)
    async def systemone(payload: SystemOneRequest, request: Request) -> dict[str, Any]:
        evaluation = asyncio.create_task(
            request.app.state.gateway.evaluate(payload, request.state.request_id)
        )

        async def wait_for_disconnect() -> None:
            while not await request.is_disconnected():
                await asyncio.sleep(0.05)

        disconnected = asyncio.create_task(wait_for_disconnect())
        try:
            done, _ = await asyncio.wait(
                {evaluation, disconnected}, return_when=asyncio.FIRST_COMPLETED
            )
            if disconnected in done and not evaluation.done():
                evaluation.cancel()
                await asyncio.gather(evaluation, return_exceptions=True)
                raise RequestError(499, "Client disconnected")
            disconnected.cancel()
            await asyncio.gather(disconnected, return_exceptions=True)
            response, diagnostics = await evaluation
        except BaseException:
            for task in (evaluation, disconnected):
                if not task.done():
                    task.cancel()
            await asyncio.gather(evaluation, disconnected, return_exceptions=True)
            raise
        _log_event(
            {
                "event": "evaluation.completed",
                "request_id": diagnostics["request_id"],
                "config_id": diagnostics["config_id"],
                "duration_ms": diagnostics["duration_ms"],
                "branches": [
                    {
                        "question_id": branch["question_id"],
                        "index": branch["index"],
                        "observed_label_mass": branch["observed_label_mass"],
                        "label_mass_upper_bound": branch["label_mass_upper_bound"],
                        "missing_label_count": len(branch["missing_labels"]),
                        "warning_count": len(branch["warnings"]),
                        "warning_codes": [
                            warning["code"] for warning in branch["warnings"]
                        ],
                    }
                    for branch in diagnostics["branches"]
                ],
                "usage": diagnostics["usage"],
            }
        )
        return response

    return app
