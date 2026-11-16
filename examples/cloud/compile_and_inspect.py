"""Compile a JSON cloud spec without credentials or cloud mutations.

Usage: python examples/cloud/compile_and_inspect.py aws examples/cloud/aws-glue.json output/aws
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from datamigration.cloud import compile_plan


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("provider", choices=["aws", "azure", "databricks"])
    parser.add_argument("spec", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    spec = json.loads(args.spec.read_text(encoding="utf-8"))
    bundle = compile_plan(args.provider, spec)
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "bundle.json").write_text(json.dumps(bundle, indent=2) + "\n")
    for name, artifact in bundle["artifacts"].items():
        (args.output / f"{name}.json").write_text(json.dumps(artifact, indent=2) + "\n")
    print(f"Compiled {args.provider} plan {bundle['plan_hash']} into {args.output}")
    print("No resources deployed and no migration started. Review every precondition in bundle.json.")


if __name__ == "__main__":
    main()
