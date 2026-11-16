"""Role-checked operations reused by protocol transports."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Literal

from pydantic import Field

from ..assistant import AssistantConfig, AssistantRequest, chat
from ..connectors import CONNECTOR_CAPABILITIES
from ..console import ConnectionRegistry, RunStatus, list_runs
from ..engine import connector, validate_plan
from ..security import ROLE_ACTIONS, Principal, RolePolicy
from ..spec import Connection, Contract, MigrationSpec
from ..workflow import RunManager


class DiscoveryRequest(Contract):
    connection: Connection
    scope: Literal["source", "target"] = "source"
    tables: list[str] | None = Field(default=None, max_length=1000)
    include_schemas: bool = True
    max_tables: int = Field(default=100, ge=1, le=1000)


def discover_connection(request: DiscoveryRequest, allowed_tables: frozenset[str] | None = None) -> dict:
    """List and inspect the configured database without fetching row samples."""
    resolved = request.connection.resolve()
    # Merely connecting to a missing SQLite file creates it. Discovery must not
    # create a destination before approval, even when its inventory is empty.
    if resolved.startswith("sqlite"):
        from sqlalchemy.engine import make_url

        if not Path(make_url(resolved).database).exists():
            if request.scope == "source":
                raise FileNotFoundError("Configured source database does not exist")
            if request.tables:
                raise ValueError("Requested tables do not exist in the unprovisioned target")
            return {
                "scope": request.scope,
                "connection_kind": request.connection.kind,
                "connection_env": request.connection.url_env,
                "table_names": [],
                "tables": [],
                "raw_samples_included": False,
                "resource_exists": False,
                "inventory_scope": "configured_database_or_namespace",
            }
    adapter = connector(request.connection)
    try:
        available = sorted(set(adapter.list_tables()))
        if allowed_tables is not None:
            available = [name for name in available if name in allowed_tables]
        selected = request.tables if request.tables is not None else available
        if len(selected) != len(set(selected)):
            raise ValueError("Discovery table names must be distinct")
        if not set(selected) <= set(available):
            raise ValueError("A requested table is absent from the configured database inventory")
        if len(selected) > request.max_tables:
            raise ValueError("Discovery table budget exceeded; select explicit tables or increase max_tables")
        tables = [asdict(adapter.inspect_table(name)) for name in selected] if request.include_schemas else []
        return {
            "scope": request.scope,
            "connection_kind": request.connection.kind,
            "connection_env": request.connection.url_env,
            "table_names": selected,
            "tables": tables,
            "raw_samples_included": False,
            "inventory_scope": "configured_database_or_namespace",
        }
    finally:
        adapter.close()


def public_status(status: dict) -> dict:
    # A viewer gets outcomes and counts, not filesystem paths or saved specs.
    return {key: value for key, value in status.items() if key != "artifact_directory"}


class MigrationService:
    def __init__(self, manager: RunManager, principal: Principal):
        self.manager = manager
        self.principal = principal

    def identity(self) -> dict:
        return {
            "subject": self.principal.subject,
            "role": self.principal.role.value,
            "actions": sorted(ROLE_ACTIONS[self.principal.role]),
        }

    def connections(self, registry: ConnectionRegistry) -> dict:
        return registry.visible_to(self.principal)

    def chat(self, request: AssistantRequest, registry: ConnectionRegistry, config: AssistantConfig) -> dict:
        return chat(self.manager.workspace, self.principal, registry, request, config)

    def runs(self, *, limit: int = 25, offset: int = 0, status: RunStatus | None = None) -> dict:
        return list_runs(self.manager.workspace, self.principal, limit=limit, offset=offset, status=status)

    def connectors(self) -> dict:
        RolePolicy.require(self.principal, "connectors")
        return {"connectors": CONNECTOR_CAPABILITIES}

    def discover(self, request: DiscoveryRequest) -> dict:
        RolePolicy.require(self.principal, "discover")
        allowed = RolePolicy.connection(self.principal, request.connection, request.tables)
        return discover_connection(request, allowed)

    def start(self, spec: MigrationSpec, run_id: str | None = None) -> dict:
        RolePolicy.require(self.principal, "plan")
        RolePolicy.spec(self.principal, spec)
        # RunManager always stops at its approval interrupt on a new run.
        return public_status(self.manager.start(spec, run_id))

    def status(self, run_id: str) -> dict:
        RolePolicy.require(self.principal, "status")
        self._run_scope(run_id)
        return public_status(self.manager.status(run_id))

    def plan(self, run_id: str) -> dict:
        RolePolicy.require(self.principal, "read_plan")
        path = self.manager.run_dir(run_id) / "plan.json"
        plan = json.loads(path.read_text(encoding="utf-8"))
        spec = validate_plan(plan)
        RolePolicy.spec(self.principal, spec)
        return plan

    def decide(self, run_id: str, *, approved: bool, plan_hash: str) -> dict:
        RolePolicy.require(self.principal, "approve")
        self._run_scope(run_id)
        # Approving checks the resolved plan and its connection binding. A
        # reviewer may still safely reject a plan whose environment has drifted.
        if approved:
            self.plan(run_id)
        # The reviewer's identity comes from server-owned authentication.
        return public_status(
            self.manager.decide(
                run_id, approved=approved, plan_hash=plan_hash, reviewer=self.principal.subject
            )
        )

    def retry(self, run_id: str) -> dict:
        RolePolicy.require(self.principal, "retry")
        self._run_scope(run_id)
        return public_status(self.manager.retry(run_id))

    def _run_scope(self, run_id: str) -> None:
        metadata = json.loads((self.manager.run_dir(run_id) / "run.json").read_text(encoding="utf-8"))
        RolePolicy.spec(self.principal, MigrationSpec.model_validate(metadata["spec"]))
