"""Authenticated single-workspace HTTP API, RBAC, and A2A 1.0 JSON-RPC.

Deploy behind TLS and enterprise identity. Server-configured scopes restrict
connection references and tables; native database permissions remain required.
"""

from __future__ import annotations

import json
import os
from typing import Any, Mapping

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from .protocols.a2a import A2AService, PROTOCOL_VERSION, agent_card, rpc_error
from .console import ConnectionRegistry, RunStatus
from .protocols.service import DiscoveryRequest, MigrationService
from .security import AuthenticationError, AuthorizationError, Principal, TokenAuthenticator
from .spec import MigrationSpec, fingerprint
from .workflow import RunManager, safe_error


class RunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    spec: MigrationSpec
    run_id: str | None = None


class ApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    approved: bool
    plan_hash: str
    # Backwards-compatible display input. The ledger always uses the identity
    # attached to the authenticated server token, never this caller-supplied text.
    reviewer: str | None = Field(default=None, max_length=200)


def create_app(
    workspace: str = ".migration",
    token: str | None = None,
    *,
    token_roles: Mapping[str, Any] | None = None,
    a2a_base_url: str | None = None,
    connection_profiles: list[dict[str, Any]] | None = None,
) -> FastAPI:
    authenticator = TokenAuthenticator.from_config(token, token_roles)
    registry = ConnectionRegistry.from_config(connection_profiles)
    manager = RunManager(workspace)
    public_url = a2a_base_url or os.environ.get("MIGRATION_PUBLIC_URL", "http://127.0.0.1:8000")
    protocol = A2AService(manager, public_url)
    card = agent_card(public_url)
    app = FastAPI(
        title="LangGraph Data Migration", version="0.1.0", docs_url=None, redoc_url=None, openapi_url=None
    )
    app.state.migration_manager = manager

    @app.exception_handler(RequestValidationError)
    async def request_validation_error(request, exc):
        return JSONResponse(
            status_code=422,
            content={
                "detail": [
                    {"loc": list(error["loc"]), "msg": error["msg"], "type": error["type"]}
                    for error in exc.errors()
                ]
            },
        )

    def authorized(authorization: str = Header(default="")) -> Principal:
        try:
            return authenticator.authenticate(authorization)
        except AuthenticationError:
            raise HTTPException(
                status_code=401, detail="Authentication required", headers={"WWW-Authenticate": "Bearer"}
            ) from None

    def service(principal: Principal = Depends(authorized)) -> MigrationService:
        return MigrationService(manager, principal)

    def call(function, *args, **kwargs) -> dict[str, Any]:
        try:
            return function(*args, **kwargs)
        except AuthorizationError:
            raise HTTPException(
                status_code=403,
                detail="The authenticated identity is not authorized for this operation or resource",
            ) from None
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="Run or planning artifact not found") from None
        except Exception as exc:
            raise HTTPException(status_code=400, detail=safe_error(exc)) from None

    @app.get("/health")
    def health():
        from .maintenance import status

        return {"status": "ok", "mode": "single_workspace_rbac", **status(manager.workspace)}

    @app.get("/connectors")
    def connectors(operations: MigrationService = Depends(service)):
        return call(operations.connectors)

    @app.get("/me")
    def get_identity(operations: MigrationService = Depends(service)):
        return call(operations.identity)

    @app.get("/connections")
    def get_connections(operations: MigrationService = Depends(service)):
        return call(operations.connections, registry)

    @app.post("/discover")
    def discover(request: DiscoveryRequest, operations: MigrationService = Depends(service)):
        return call(operations.discover, request)

    @app.post("/runs")
    def start_run(request: RunRequest, operations: MigrationService = Depends(service)):
        return call(operations.start, request.spec, request.run_id)

    @app.get("/runs")
    def get_runs(
        limit: int = Query(default=25, ge=1, le=100),
        offset: int = Query(default=0, ge=0, le=5000),
        status: RunStatus | None = None,
        operations: MigrationService = Depends(service),
    ):
        return call(operations.runs, limit=limit, offset=offset, status=status)

    @app.get("/runs/{run_id}")
    def get_run(run_id: str, operations: MigrationService = Depends(service)):
        return call(operations.status, run_id)

    @app.get("/runs/{run_id}/plan")
    def get_plan(run_id: str, operations: MigrationService = Depends(service)):
        return call(operations.plan, run_id)

    @app.post("/runs/{run_id}/approval")
    def approve(run_id: str, request: ApprovalRequest, operations: MigrationService = Depends(service)):
        return call(operations.decide, run_id, approved=request.approved, plan_hash=request.plan_hash)

    @app.post("/runs/{run_id}/retry")
    def retry(run_id: str, operations: MigrationService = Depends(service)):
        return call(operations.retry, run_id)

    @app.get("/.well-known/agent-card.json")
    def get_agent_card(if_none_match: str | None = Header(default=None)):
        etag = '"' + fingerprint(card) + '"'
        headers = {"Cache-Control": "public, max-age=300", "ETag": etag}
        if if_none_match == etag:
            return Response(status_code=304, headers=headers)
        return JSONResponse(card, headers=headers)

    @app.post("/a2a")
    async def a2a_rpc(
        request: Request, background: BackgroundTasks, principal: Principal = Depends(authorized)
    ):
        headers = {"A2A-Version": PROTOCOL_VERSION}
        raw = await request.body()
        if len(raw) > 1_000_000:
            raise HTTPException(status_code=413, detail="A2A planning request exceeds the input budget")
        try:
            payload = json.loads(raw)
        except (ValueError, UnicodeError):
            return JSONResponse(rpc_error(None, -32700, "Invalid JSON payload"), headers=headers)
        try:
            result = await run_in_threadpool(
                protocol.dispatch,
                payload,
                principal,
                version=request.headers.get("a2a-version", ""),
                schedule=background.add_task,
            )
        except AuthorizationError:
            raise HTTPException(
                status_code=403,
                detail="The authenticated identity is not authorized for this operation or resource",
            ) from None
        if (
            isinstance(payload, dict)
            and payload.get("jsonrpc") == "2.0"
            and isinstance(payload.get("method"), str)
            and "id" not in payload
            and result.get("error", {}).get("code") != -32600
        ):
            return Response(status_code=204, headers=headers)
        return JSONResponse(result, headers=headers)

    return app
