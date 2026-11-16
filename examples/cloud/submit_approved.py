"""Submit an already reviewed bundle to already provisioned cloud resources.

This command DOES start a cloud job and can incur cloud charges/write target data.
It requires a complete bundle SHA256 approved by the operator and a stable operation
key. Persist the ledger and receipt. Cloud job success still needs reconciliation.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from datamigration.cloud import get_executor
from datamigration.cloud.base import digest


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("bundle", type=Path)
    parser.add_argument("--approve-bundle-sha256", required=True)
    parser.add_argument("--operation-key", required=True)
    parser.add_argument("--ledger", default=".migration/cloud-submissions.sqlite3")
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--region")
    parser.add_argument("--databricks-host")
    args = parser.parse_args()
    bundle = json.loads(args.bundle.read_text(encoding="utf-8"))
    actual = digest(bundle)
    if actual != args.approve_bundle_sha256:
        parser.error(f"Bundle approval mismatch. Reviewed bundle SHA256 must equal {actual}")
    config = dict(bundle["submission"], ledger_path=args.ledger)
    if args.region:
        config["region"] = args.region
    if args.databricks_host:
        config["host"] = args.databricks_host
    executor = get_executor(bundle["provider"], config)
    execution_id = executor.start(bundle, args.operation_key)
    args.receipt.parent.mkdir(parents=True, exist_ok=True)
    args.receipt.write_text(json.dumps({"provider": bundle["provider"], "execution_id": execution_id,
                                       "bundle_hash": actual, "operation_key": args.operation_key}, indent=2) + "\n")
    print(f"Submitted {execution_id}. Receipt: {args.receipt}")


if __name__ == "__main__":
    main()
