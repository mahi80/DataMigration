"""Publish a reviewed catalog to an existing OpenMetadata database schema.

Example:
  python examples/governance/publish_catalog.py \
    --catalog catalog.json --schema warehouse.commerce.public \
    --server https://metadata.example.com --approve

Set OPENMETADATA_TOKEN in the process environment. This example never creates
services or database schemas, and it never publishes source data samples.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from datamigration.governance import OpenMetadataClient


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--schema", required=True, help="Existing service.database.schema FQN")
    parser.add_argument("--server", required=True, help="OpenMetadata origin or /api base URL")
    parser.add_argument("--approve", action="store_true", help="Publish the reviewed catalog now")
    args = parser.parse_args()
    if not args.approve:
        parser.error("Review the catalog and pass --approve to publish it")
    catalog = json.loads(args.catalog.read_text(encoding="utf-8"))
    with OpenMetadataClient(args.server) as client:
        entities = client.publish_catalog(catalog, args.schema, approved=True)
    print(
        json.dumps(
            [{"id": entity["id"], "fullyQualifiedName": entity["fullyQualifiedName"]} for entity in entities],
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
