"""Launch a local console with real synthetic migration runs.

From the repository root, install the API extra and frontend dependencies first:
    python examples/console/serve_demo.py --directory .demo/console

Only new synthetic data is created. The API binds to loopback. This demonstration
uses role tokens, not an enterprise identity provider, and never connects to an
external database. Existing demo directories are not overwritten.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import secrets
import shutil
import signal
import socket
import subprocess
import tempfile
import threading

import uvicorn

from datamigration.api import create_app
from datamigration.demo import seed_demo
from datamigration.spec import MigrationSpec
from datamigration.workflow import RunManager


E2E_TOKENS = {role: f"synthetic-console-{role}-token-for-local-e2e-only"
              for role in ("admin", "planner", "approver", "viewer", "operator")}


def check_port(port: int) -> None:
    if not 1 <= port <= 65535:
        raise ValueError("Choose a port between 1 and 65535")
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", port))


def stop_console(process: subprocess.Popen) -> None:
    # npm starts a shell and a Next child; stop the whole owned process group.
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
        process.wait(timeout=5)
    except ProcessLookupError:
        pass


def seed_workspace(directory: Path):
    if directory.exists():
        raise FileExistsError("Choose a new demo directory; existing data will not be overwritten")
    directory.mkdir(parents=True, mode=0o700)
    base, _ = seed_demo(directory / "data", star=True)
    manager = RunManager(directory / "state")
    base.name = "Commerce star schema"
    planned = manager.start(base, "commerce-star")
    manager.decide("commerce-star", approved=True, plan_hash=planned["plan_hash"], reviewer="synthetic-fixture-only")

    def spec(name: str, source: str, target: str, *, quality_rules=None) -> MigrationSpec:
        raw = base.model_dump(mode="json")
        raw.update(name=name, workload="oltp", tables=[{
            "source": source, "target": target, "keys": ["id"],
            "quality_rules": quality_rules or [],
        }])
        return MigrationSpec.model_validate(raw)

    planned = manager.start(spec("Order history", "orders", "orders"), "orders-validated")
    manager.decide("orders-validated", approved=True, plan_hash=planned["plan_hash"], reviewer="synthetic-fixture-only")
    manager.start(spec("Customer directory", "customers", "customer_directory"), "customers-review")
    manager.start(spec("Product catalog", "products", "product_catalog"), "products-review")
    planned = manager.start(spec("Price policy check", "products", "price_review", quality_rules=[{
        "name": "synthetic-zero-budget", "column": "unit_price_cents", "check": "range", "maximum": "0",
    }]), "price-policy")
    try:
        manager.decide("price-policy", approved=True, plan_hash=planned["plan_hash"], reviewer="synthetic-fixture-only")
    except ValueError:
        if manager.status("price-policy")["status"] != "FAILED":
            raise

    profiles = [
        {"id": "demo-source", "label": "Demo commerce source", "kind": "sqlite", "url_env": base.source.url_env,
         "options": {}, "description": "Synthetic customers, orders, products and dimensional tables."},
        {"id": "demo-target", "label": "Demo analytics target", "kind": "sqlite", "url_env": base.target.url_env,
         "options": {}, "description": "A separate local destination used only by this demonstration."},
    ]
    return manager.workspace, profiles


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", default=".demo/console")
    parser.add_argument("--api-port", type=int, default=8000)
    parser.add_argument("--ui-port", type=int, default=3000)
    parser.add_argument("--api-only", action="store_true")
    parser.add_argument("--e2e", action="store_true", help="Isolated temporary synthetic fixture for browser tests")
    args = parser.parse_args()
    check_port(args.api_port)
    frontend = Path(__file__).resolve().parents[2] / "frontend"
    npm = None
    if not args.api_only:
        if args.api_port == args.ui_port:
            raise ValueError("The API and console need different ports")
        check_port(args.ui_port)
        npm = shutil.which("npm")
        if not npm or not (frontend / "node_modules/next").exists():
            raise RuntimeError("Install Node.js and run npm ci in frontend before launching the console")
    temporary = tempfile.TemporaryDirectory(prefix="migration-console-e2e-") if args.e2e else None
    directory = Path(temporary.name) / "fixture" if temporary else Path(args.directory).resolve()
    console = None
    try:
        workspace, profiles = seed_workspace(directory)
        tokens = E2E_TOKENS if args.e2e else {role: secrets.token_urlsafe(32) for role in E2E_TOKENS}
        identities = {token: {"subject": f"Demo {role}", "role": role} for role, token in tokens.items()}
        app = create_app(str(workspace), token_roles=identities, connection_profiles=profiles)
        if not args.e2e:
            access = directory / "demo-access.json"
            with access.open("x", encoding="utf-8") as stream:
                os.chmod(access, 0o600)
                json.dump({"synthetic_data_only": True, "tokens": tokens}, stream, indent=2)
            print(f"\nLocal synthetic console: http://localhost:{args.ui_port}", flush=True)
            print(f"Role tokens are saved privately in {access}", flush=True)
            print(f"Demo administrator token: {tokens['admin']}\n", flush=True)
        if not args.api_only:
            environment = {**os.environ,
                "MIGRATION_API_URL": f"http://127.0.0.1:{args.api_port}",
                "MIGRATION_UI_ORIGIN": f"http://localhost:{args.ui_port}",
                "MIGRATION_UI_SESSION_SECRET": secrets.token_hex(32),
                "NEXT_TELEMETRY_DISABLED": "1",
            }
            console = subprocess.Popen([npm, "run", "dev", "--", "--port", str(args.ui_port)],
                                       cwd=frontend, env=environment, start_new_session=os.name == "posix")
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=args.api_port, access_log=False))
        if console is not None:
            def watch_console() -> None:
                console.wait()
                if not server.should_exit:
                    print("The console process exited; stopping the local demo API.", flush=True)
                    server.should_exit = True
            threading.Thread(target=watch_console, daemon=True).start()
        server.run()
    finally:
        if console is not None:
            stop_console(console)
        if temporary:
            temporary.cleanup()


if __name__ == "__main__":
    main()
