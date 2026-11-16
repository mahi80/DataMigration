"""Run a synthetic typed CSV-to-Parquet migration in a fresh directory.

From the repository root:
    python examples/flatfiles/run_example.py --directory .demo/typed-files

Install the ``files`` extra. Automatic approval in this script applies only to
the synthetic rows created here; use the normal run/approve CLI for real data.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import yaml

from datamigration.spec import MigrationSpec
from datamigration.workflow import RunManager


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", default=".demo/typed-files")
    args = parser.parse_args()
    directory = Path(args.directory).resolve()
    directory.mkdir(parents=True, exist_ok=False)
    source, target = directory / "source", directory / "target"
    source.mkdir()
    (source / "customers.csv").write_text(
        "customer_id,name,amount,created_at,notes\n"
        "001,Asha Verma,12345.67,2026-11-01T10:00:00+00:00,\\N\n"
        "002,Ravi Shah,89.10,2026-11-02T11:30:00+00:00,synthetic\n",
        encoding="utf-8",
    )
    os.environ["EXAMPLE_FILE_SOURCE_ROOT"] = str(source)
    os.environ["EXAMPLE_FILE_TARGET_ROOT"] = str(target)
    columns = [
        {"name": "customer_id", "data_type": "string", "nullable": False},
        {"name": "name", "data_type": "string"},
        {"name": "amount", "data_type": "decimal", "precision": 18, "scale": 2},
        {"name": "created_at", "data_type": "datetime", "timezone": "UTC"},
        {"name": "notes", "data_type": "string"},
    ]
    spec = MigrationSpec.model_validate({
        "name": "synthetic-csv-to-parquet",
        "source": {"kind": "csv", "url_env": "EXAMPLE_FILE_SOURCE_ROOT",
                   "options": {"schemas": {"customers": {"columns": columns, "primary_key": ["customer_id"]}}}},
        "target": {"kind": "parquet", "url_env": "EXAMPLE_FILE_TARGET_ROOT"},
        "tables": [{"source": "customers", "target": "customers", "keys": ["customer_id"]}],
        "policy": {"source_quiesced": True, "batch_size": 2},
    })
    path = directory / "migration.yaml"
    path.write_text(yaml.safe_dump(spec.model_dump(mode="json"), sort_keys=False), encoding="utf-8")
    manager = RunManager(directory / "state")
    planned = manager.start(spec)
    completed = manager.decide(planned["run_id"], approved=True, plan_hash=planned["plan_hash"],
                               reviewer="synthetic-file-example-only")
    print(json.dumps({"run": completed, "generated_spec": str(path), "source_root": str(source),
                      "target_root": str(target), "synthetic_data_only": True}, indent=2))


if __name__ == "__main__":
    main()
