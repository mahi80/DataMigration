"""Official MCP SDK stdio server with startup-bound authorization.

Install datamigration-agent[mcp]. The v1 SDK is intentionally pinned because
this entry point uses mcp.server.fastmcp, which moved in the v2 SDK.
"""

from __future__ import annotations

import argparse
import os
from typing import Any

from ..security import Principal, RolePolicy, local_principal
from ..spec import MigrationSpec
from ..workflow import RunManager
from .service import DiscoveryRequest, MigrationService


_TOOLS = {
    "list_connectors": ("connectors", set()),
    "discover_tables": ("discover", {"request"}),
    "plan_migration": ("plan", {"spec", "run_id"}),
    "get_run_status": ("status", {"run_id"}),
    "get_migration_plan": ("read_plan", {"run_id"}),
}


def create_mcp_server(workspace: str = ".migration", *, principal: Principal | None = None):
    """Build an MCP server without opening a transport or a database connection."""
    try:
        from mcp.server.fastmcp import FastMCP
        from mcp.server.fastmcp.exceptions import ToolError
        from mcp.types import ToolAnnotations
    except ImportError:
        raise RuntimeError("Install the optional MCP dependency: mcp>=1.28,<2") from None

    identity = principal if principal is not None else local_principal()
    service = MigrationService(RunManager(workspace), identity)

    class AuthorizedMCP(FastMCP):
        async def list_tools(self):
            return [
                tool
                for tool in await super().list_tools()
                if RolePolicy.allows(identity, _TOOLS[tool.name][0])
            ]

        async def call_tool(self, name: str, arguments: dict[str, Any]):
            if name not in _TOOLS:
                raise ToolError("Unknown migration tool")
            action, fields = _TOOLS[name]
            if not RolePolicy.allows(identity, action):
                raise ToolError("The authenticated role is not authorized for this tool")
            if not isinstance(arguments, dict) or not set(arguments) <= fields:
                raise ToolError("Unexpected tool arguments; identity and role are configured on the server")
            try:
                return await super().call_tool(name, arguments)
            except Exception:
                # SDK argument errors can contain Pydantic input values. Do not
                # reflect credentials or untrusted schema text into MCP errors.
                raise ToolError("Migration tool failed; check validated inputs and the run status") from None

    server = AuthorizedMCP(
        "DataMigration",
        instructions=(
            "Discover schemas and prepare reviewed migration plans. A new plan stops for "
            "approval. No tool can approve a plan, execute arbitrary SQL, run shell commands, "
            "or change its server-configured role. Use the authenticated approval API separately."
        ),
    )
    read_only = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True)

    @server.tool(annotations=read_only)
    def list_connectors() -> dict[str, Any]:
        """List implemented connector capabilities and their verification levels."""
        return service.connectors()

    @server.tool(annotations=read_only)
    def discover_tables(request: DiscoveryRequest) -> dict[str, Any]:
        """Inspect a source or target Connection; return table schemas without row samples.

        request requires connection={kind,url_env,options}; optional scope is
        source or target. tables selects names and max_tables bounds discovery.
        Credentials must already exist in the server environment.
        """
        return service.discover(request)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False))
    def plan_migration(spec: MigrationSpec, run_id: str | None = None) -> dict[str, Any]:
        """Start a schema-driven migration plan and stop at human approval.

        Accepts a versioned MigrationSpec. This creates durable local planning
        artifacts and never grants destination-write approval.
        """
        return service.start(spec, run_id)

    @server.tool(annotations=read_only)
    def get_run_status(run_id: str) -> dict[str, Any]:
        """Read a run's lifecycle and validation outcome without its connection spec."""
        return service.status(run_id)

    @server.tool(annotations=read_only)
    def get_migration_plan(run_id: str) -> dict[str, Any]:
        """Read the exact integrity-checked plan that a separate approver must review."""
        return service.plan(run_id)

    return server


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", default=os.environ.get("MIGRATION_STATE_DIR", ".migration"))
    args = parser.parse_args()
    create_mcp_server(args.workspace).run(transport="stdio")


if __name__ == "__main__":
    main()
