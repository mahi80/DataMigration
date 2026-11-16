"""Run a reviewed CTE source over synthetic SQLite data in a fresh directory.

From the repository root:
    python examples/queries/run_example.py --directory .demo/reviewed-query

Install the ``queries`` extra. The connector enables SQLite's query_only mode.
Production SQL endpoints require a database-enforced read-only identity.
Automatic approval applies only to synthetic rows created by this example.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sqlite3

import yaml

from datamigration.connectors import create_connector
from datamigration.connectors.sql_inventory import inventory_sql
from datamigration.spec import MigrationSpec
from datamigration.workflow import RunManager


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", default=".demo/reviewed-query")
    args = parser.parse_args()
    directory = Path(args.directory).resolve()
    directory.mkdir(parents=True, exist_ok=False)
    source, target = directory / "source.sqlite", directory / "target.sqlite"
    with sqlite3.connect(source) as db:
        db.executescript("""
            CREATE TABLE orders(id INTEGER PRIMARY KEY, customer_id INTEGER NOT NULL, amount_cents INTEGER NOT NULL);
            INSERT INTO orders VALUES (1,101,5000),(2,101,6000),(3,202,3000);
            CREATE VIEW order_totals AS
              SELECT customer_id, SUM(amount_cents) AS total_cents FROM orders GROUP BY customer_id;
        """)
    os.environ["EXAMPLE_QUERY_SOURCE_URL"] = f"sqlite:///{source}"
    os.environ["EXAMPLE_QUERY_TARGET_URL"] = f"sqlite:///{target}"
    query = {
        "sql": """WITH totals AS (
            SELECT customer_id, SUM(amount_cents) AS total_cents
            FROM orders GROUP BY customer_id
        )
        SELECT customer_id, total_cents FROM totals
        WHERE total_cents >= :minimum_cents ORDER BY customer_id""",
        "parameters": {"minimum_cents": 10000},
        "columns": [{"name": "customer_id", "data_type": "integer", "nullable": False},
                    {"name": "total_cents", "data_type": "integer", "nullable": False}],
        "keys": ["customer_id"],
    }
    spec = MigrationSpec.model_validate({
        "name": "synthetic-reviewed-cte",
        "source": {"kind": "sqlite", "url_env": "EXAMPLE_QUERY_SOURCE_URL",
                   "options": {"read_only_queries": True, "queries": {"high_value_customers": query},
                               "query_views": ["order_totals"]}},
        "target": {"kind": "sqlite", "url_env": "EXAMPLE_QUERY_TARGET_URL"},
        "tables": [{"source": "high_value_customers", "target": "customer_totals", "keys": ["customer_id"]}],
        "policy": {"source_quiesced": True, "batch_size": 2},
    })
    path = directory / "migration.yaml"
    path.write_text(yaml.safe_dump(spec.model_dump(mode="json"), sort_keys=False), encoding="utf-8")
    adapter = create_connector("sqlite", os.environ["EXAMPLE_QUERY_SOURCE_URL"], spec.source.options)
    try:
        inventory = inventory_sql(adapter, include_definitions=True)
    finally:
        adapter.close()
    (directory / "inventory.json").write_text(json.dumps(inventory, indent=2), encoding="utf-8")
    manager = RunManager(directory / "state")
    planned = manager.start(spec)
    completed = manager.decide(planned["run_id"], approved=True, plan_hash=planned["plan_hash"],
                               reviewer="synthetic-query-example-only")
    print(json.dumps({"run": completed, "generated_spec": str(path), "source_url": os.environ["EXAMPLE_QUERY_SOURCE_URL"],
                      "target_url": os.environ["EXAMPLE_QUERY_TARGET_URL"], "synthetic_data_only": True}, indent=2))


if __name__ == "__main__":
    main()
