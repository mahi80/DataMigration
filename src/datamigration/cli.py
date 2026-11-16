"""Command-line entry point. No cloud resources are deployed implicitly."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import yaml

from .spec import Connection, fingerprint, load_spec, reject_inline_secrets, write_json
from .workflow import RunManager, safe_error


def raw_spec(path: str) -> dict:
    result = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(result, dict):
        raise ValueError("Expected a YAML/JSON object")
    return result


def output(value):
    print(json.dumps(value, indent=2, ensure_ascii=False))


def selected_connection(path: str, side: str) -> Connection:
    document = raw_spec(path)
    value = document[side] if "source" in document and "target" in document else document
    return Connection.model_validate(value)


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="datamigrate", description="Governed LangGraph data migration")
    sub = p.add_subparsers(dest="command", required=True)

    def workspace(command):
        command.add_argument("--workspace", default=os.environ.get("MIGRATION_STATE_DIR", ".migration"), help="Private state/artifact directory")

    demo = sub.add_parser("demo", help="Migrate new synthetic data, approve it, and reconcile it")
    demo.add_argument("--directory", default=".demo/quickstart")
    demo.add_argument("--run-id")
    demo.add_argument("--star", action="store_true", help="Use explicitly materialized dimensions and fact tables")
    demo.add_argument("--delta", action="store_true", help="Use a local Delta Lake target (install delta extra)")
    workspace(demo)
    run = sub.add_parser("run", help="Discover and plan; pause before destination writes")
    run.add_argument("spec")
    run.add_argument("--run-id")
    workspace(run)
    for name in ("approve", "reject"):
        command = sub.add_parser(name)
        command.add_argument("run_id")
        command.add_argument("--plan-hash", required=True)
        command.add_argument("--reviewer", required=True)
        workspace(command)
    for name in ("status", "retry"):
        command = sub.add_parser(name)
        command.add_argument("run_id")
        workspace(command)
    plan = sub.add_parser("plan", help="Export a reviewable snapshot plan without starting a run")
    plan.add_argument("spec")
    plan.add_argument("--output", default="artifacts/plan.json")
    sub.add_parser("connectors", help="Print truthful connector capabilities and test status")
    for name in ("discover", "inspect-sql"):
        command = sub.add_parser(name, help="Read source or target metadata without copying data")
        command.add_argument("spec", help="Migration spec or one Connection YAML/JSON object")
        command.add_argument("--side", choices=("source", "target"), default="source")
        command.add_argument("--output")
        if name == "discover":
            command.add_argument("--table", action="append", dest="tables", help="Repeat to select tables")
            command.add_argument("--max-tables", type=int, default=100)
            command.add_argument("--names-only", action="store_true", help="List inventory names without inspecting column types")
        else:
            command.add_argument("--include-definitions", action="store_true", help="Include authorized SQL view/procedure definitions in output")
    maintenance = sub.add_parser("maintenance", help="Pause new work and inspect running operations before deployment")
    maintenance.add_argument("action", choices=("on", "off", "status"))
    workspace(maintenance)
    serve = sub.add_parser("serve", help="Start the authenticated RBAC API and A2A endpoint")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--public-url", help="Externally reachable base URL advertised in the A2A Agent Card")
    workspace(serve)
    mcp = sub.add_parser("mcp", help="Start the MCP stdio server with a startup-bound identity")
    workspace(mcp)

    cloud = sub.add_parser("cloud", help="Compile and orchestrate approved native cloud jobs")
    cs = cloud.add_subparsers(dest="cloud_command", required=True)
    for name in ("compile", "run"):
        command = cs.add_parser(name)
        command.add_argument("provider", choices=("aws", "azure", "databricks"))
        command.add_argument("spec")
        if name == "compile":
            command.add_argument("--output", required=True)
        else:
            command.add_argument("--run-id")
            workspace(command)
    for name in ("approve", "reject", "status", "poll", "retry"):
        command = cs.add_parser(name)
        command.add_argument("run_id")
        if name in {"approve", "reject"}:
            command.add_argument("--bundle-hash", required=True)
            command.add_argument("--reviewer", required=True)
        workspace(command)

    ontology = sub.add_parser("ontology", help="Export an offline Ontop R2RML/OWL package")
    ontology.add_argument("spec")
    ontology.add_argument("--output", default="artifacts/ontop")
    ontology.add_argument("--base-iri", default="https://example.org/migration/")
    ontology.add_argument("--approved", action="store_true", help="Label exported mappings as operator-reviewed")

    governance = sub.add_parser("publish-catalog", help="Publish a reviewed catalog to an existing OpenMetadata schema")
    governance.add_argument("catalog")
    governance.add_argument("--schema-fqn", required=True)
    governance.add_argument("--base-url-env", default="OPENMETADATA_URL")
    governance.add_argument("--token-env", default="OPENMETADATA_TOKEN")
    governance.add_argument("--catalog-hash", required=True, help="SHA-256 printed by catalog-hash, confirming exact content review")
    catalog_hash = sub.add_parser("catalog-hash")
    catalog_hash.add_argument("catalog")
    return p


def dispatch(args):
    if args.command in {"discover", "inspect-sql"}:
        connection = selected_connection(args.spec, args.side)
        if args.command == "discover":
            from .protocols.service import DiscoveryRequest, discover_connection
            result = discover_connection(DiscoveryRequest(connection=connection, scope=args.side, tables=args.tables, max_tables=args.max_tables, include_schemas=not args.names_only))
        else:
            from .engine import connector
            from .connectors.sql_inventory import inventory_sql
            adapter = connector(connection)
            try:
                result = inventory_sql(adapter, include_definitions=args.include_definitions)
            finally:
                adapter.close()
        if args.output:
            write_json(Path(args.output), result)
            return {"metadata_path": str(Path(args.output).resolve()), "copied_data": False}
        return result
    if args.command == "mcp":
        from .protocols.mcp_server import create_mcp_server
        create_mcp_server(args.workspace).run(transport="stdio")
        return None
    if args.command == "maintenance":
        from .maintenance import set_maintenance, status
        return status(args.workspace) if args.action == "status" else set_maintenance(args.workspace, args.action == "on")
    if args.command == "connectors":
        from .connectors import CONNECTOR_CAPABILITIES
        return CONNECTOR_CAPABILITIES
    if args.command == "demo":
        from .demo import seed_demo
        spec, path = seed_demo(args.directory, star=args.star, delta=args.delta)
        manager = RunManager(args.workspace)
        state = manager.start(spec, args.run_id)
        state = manager.decide(state["run_id"], approved=True, plan_hash=state["plan_hash"], reviewer="synthetic-demo-only")
        return {**state, "synthetic_data_only": True, "generated_spec": str(path)}
    if args.command == "run":
        return RunManager(args.workspace).start(load_spec(args.spec), args.run_id)
    if args.command in {"approve", "reject"}:
        return RunManager(args.workspace).decide(args.run_id, approved=args.command == "approve", plan_hash=args.plan_hash, reviewer=args.reviewer)
    if args.command in {"status", "retry"}:
        return getattr(RunManager(args.workspace), args.command)(args.run_id)
    if args.command == "plan":
        from .engine import build_plan, discover
        spec = load_spec(args.spec)
        catalog = discover(spec)
        plan = build_plan(spec, catalog)
        path = Path(args.output)
        write_json(path, plan)
        write_json(path.with_name(path.stem + "-catalog.json"), catalog)
        return {"plan_hash": plan["plan_hash"], "plan_path": str(path.resolve()), "executed": False}
    if args.command == "cloud":
        if args.cloud_command == "compile":
            from .cloud import compile_plan
            spec = reject_inline_secrets(raw_spec(args.spec))
            bundle = compile_plan(args.provider, spec)
            write_json(Path(args.output), bundle)
            return {"bundle_hash": fingerprint(bundle), "bundle_path": str(Path(args.output).resolve()), "deployed": False}
        from .cloud_runs import CloudRunManager
        manager = CloudRunManager(args.workspace)
        if args.cloud_command == "run":
            return manager.start(args.provider, raw_spec(args.spec), args.run_id)
        if args.cloud_command in {"approve", "reject"}:
            return manager.approve(args.run_id, args.bundle_hash, args.reviewer, approved=args.cloud_command == "approve")
        return getattr(manager, args.cloud_command)(args.run_id)
    if args.command == "ontology":
        from .governance import export_ontop
        spec = raw_spec(args.spec)
        if "spec" in spec and "plan_hash" in spec:
            spec = spec["spec"]
        result = export_ontop(spec, Path(args.output), args.base_iri, approved=args.approved)
        return {key: str(value) for key, value in result.items()}
    if args.command == "catalog-hash":
        return {"catalog_hash": fingerprint(raw_spec(args.catalog))}
    if args.command == "publish-catalog":
        from .governance import OpenMetadataClient
        catalog = raw_spec(args.catalog)
        if fingerprint(catalog) != args.catalog_hash:
            raise ValueError("Catalog hash does not match reviewed content")
        base_url = os.environ.get(args.base_url_env)
        if not base_url:
            raise ValueError(f"Set {args.base_url_env} to the OpenMetadata API base URL")
        with OpenMetadataClient(base_url, args.token_env) as client:
            return {"published": client.publish_catalog(catalog, args.schema_fqn, approved=True)}
    if args.command == "serve":
        import uvicorn
        from .api import create_app
        uvicorn.run(create_app(args.workspace, a2a_base_url=args.public_url), host=args.host, port=args.port)
        return None
    raise ValueError("Unknown command")


def main(argv: list[str] | None = None) -> None:
    args = parser().parse_args(argv)
    try:
        result = dispatch(args)
        if result is not None:
            output(result)
    except Exception as exc:
        print(json.dumps({"error": safe_error(exc), "type": type(exc).__name__}), file=sys.stderr)
        raise SystemExit(1) from None
