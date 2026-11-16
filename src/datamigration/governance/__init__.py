"""Optional catalog publication and virtual-knowledge-graph artifacts.

These integrations do not grant database permissions or execute a migration.
"""

from .ontop import export_ontop, write_jdbc_properties_from_env
from .openmetadata import (
    OpenMetadataClient,
    OpenMetadataError,
    build_lineage_payload,
    build_table_payload,
)

__all__ = [
    "OpenMetadataClient",
    "OpenMetadataError",
    "build_lineage_payload",
    "build_table_payload",
    "export_ontop",
    "write_jdbc_properties_from_env",
]
