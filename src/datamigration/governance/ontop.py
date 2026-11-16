"""Generate inspectable R2RML/OWL files over migrated SQL tables.

This module does not connect to a database, infer business meaning, run an Ontop
server, or copy data. SQL projections read the already-transformed target tables.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit


_SQL_BACKENDS = {
    "postgresql",
    "postgres",
    "redshift",
    "databricks",
    "mysql",
    "mariadb",
    "sqlserver",
    "mssql",
    "oracle",
    "db2",
    "snowflake",
    "duckdb",
}
_XSD_TYPES = {
    "integer": "integer",
    "float": "double",
    "decimal": "decimal",
    "string": "string",
    "boolean": "boolean",
    "datetime": "dateTime",
    "date": "date",
}


def _literal(value: str) -> str:
    # JSON string escapes are valid for the strings emitted here in Turtle.
    return json.dumps(value, ensure_ascii=False)


def _name(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a nonempty string")
    if any(ord(character) < 32 for character in value):
        raise ValueError(f"{label} cannot contain control characters")
    return value


def quote_sql_identifier(identifier: str, backend: str) -> str:
    """Quote one literal identifier; never treats it as an SQL expression."""
    identifier = _name(identifier, "SQL identifier")
    if backend in {"mysql", "mariadb", "databricks"}:
        return "`" + identifier.replace("`", "``") + "`"
    if backend in {"sqlserver", "mssql"}:
        return "[" + identifier.replace("]", "]]") + "]"
    return '"' + identifier.replace('"', '""') + '"'


def _qualified_identifier(name: str, backend: str) -> str:
    # The public migration spec uses dot-separated qualification. Literal dots
    # inside an individual identifier are deliberately not guessed here.
    parts = _name(name, "target table").split(".")
    if any(not part.strip() or part != part.strip() for part in parts):
        raise ValueError("Target table qualification cannot contain empty or padded parts")
    return ".".join(quote_sql_identifier(part, backend) for part in parts)


def _base_iri(value: str) -> str:
    parts = urlsplit(_name(value, "base_iri"))
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise ValueError("base_iri must be an absolute HTTP(S) IRI")
    if parts.query or parts.fragment or parts.username or parts.password:
        raise ValueError("base_iri cannot contain credentials, query, or fragment")
    if any(character in value for character in '<>"{}\\ '):
        raise ValueError("base_iri contains characters that need percent encoding")
    return value.rstrip("/") + "/"


def export_ontop(
    spec: dict[str, Any],
    output_dir: str | Path,
    base_iri: str = "https://example.org/migration/",
    *,
    approved: bool = False,
) -> dict[str, str]:
    """Export mappings, an ontology, an example JDBC file, and a manifest.

    ``keys`` are target-column names. Target columns must be explicit and the
    declared key must be non-null and unique in the deployed database. Composite
    keys become separate URI path segments; their names are never concatenated
    without delimiters. Ontop performs R2RML IRI-safe value encoding at query time.

    The output is an offline preview unless ``approved=True`` is supplied by the
    caller's approval workflow. This boolean records intent; it is not a signed
    proof of approval or permission to expose a SPARQL endpoint.
    """
    target = spec.get("target", {})
    backend = str(target.get("kind", "")).lower()
    if backend not in _SQL_BACKENDS:
        raise ValueError(
            "Ontop export requires a supported SQL-facing target. For MongoDB, "
            "Cassandra, Redis, or raw Delta paths, first publish an approved "
            "relational projection or a Databricks SQL table. SQLite is not an "
            "advertised Ontop backend."
        )
    base = _base_iri(base_iri)
    tables = spec.get("tables")
    if not isinstance(tables, list) or not tables:
        raise ValueError("Ontop export requires explicit table mappings")
    prefixes = (
        "@prefix rr: <http://www.w3.org/ns/r2rml#> .\n"
        "@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .\n"
        "@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .\n"
        "@prefix owl: <http://www.w3.org/2002/07/owl#> .\n"
        "@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .\n\n"
    )
    mappings: list[str] = [prefixes]
    ontology: list[str] = [prefixes, f"<{base}ontology> a owl:Ontology .\n\n"]
    names: set[str] = set()
    manifest_tables: list[dict[str, Any]] = []
    for table in tables:
        table_name = _name(table.get("target"), "target table")
        if table_name in names:
            raise ValueError("Ontop export requires distinct target tables")
        names.add(table_name)
        columns = table.get("columns")
        keys = table.get("keys")
        if not isinstance(columns, list) or not columns:
            raise ValueError(f"{table_name}: explicit target columns are required")
        if not isinstance(keys, list) or not keys or len(set(keys)) != len(keys):
            raise ValueError(f"{table_name}: distinct explicit target keys are required")
        column_names = [_name(column.get("target"), "target column") for column in columns]
        if len(set(column_names)) != len(column_names):
            raise ValueError(f"{table_name}: duplicate target columns")
        if not set(keys).issubset(column_names):
            raise ValueError(f"{table_name}: a key is absent from target columns")
        for column in columns:
            if column["target"] in keys and column.get("nullable", True) is not False:
                raise ValueError(f"{table_name}: Ontop subject key must explicitly be non-null")
            if column.get("data_type") not in _XSD_TYPES:
                raise ValueError(
                    f"{table_name}.{column['target']}: direct Ontop export of "
                    f"{column.get('data_type')!r} is unsupported; define a SQL view "
                    "with an explicit scalar representation first"
                )
        # Simple unquoted aliases avoid braces, punctuation, and business field
        # names in R2RML templates; Ontop resolves SQL identifier case by dialect.
        aliases = {name: f"dm_col_{index}" for index, name in enumerate(column_names)}
        projection = ", ".join(
            f"{quote_sql_identifier(name, backend)} AS {aliases[name]}" for name in column_names
        )
        query = f"SELECT {projection} FROM {_qualified_identifier(table_name, backend)}"
        table_segment = quote(table_name, safe="")
        class_iri = f"{base}class/{table_segment}"
        template = f"{base}entity/{table_segment}/" + "/".join("{" + aliases[key] + "}" for key in keys)
        ontology.append(f"<{class_iri}> a owl:Class ; rdfs:label {_literal(table_name)} .\n")
        predicate_maps: list[str] = []
        for column in columns:
            name = column["target"]
            property_iri = f"{base}property/{table_segment}/{quote(name, safe='')}"
            datatype = _XSD_TYPES[column["data_type"]]
            predicate_maps.append(
                "    rr:predicateObjectMap [\n"
                f"        rr:predicate <{property_iri}> ;\n"
                f"        rr:objectMap [ rr:column {_literal(aliases[name])} ; "
                f"rr:datatype xsd:{datatype} ]\n"
                "    ]"
            )
            ontology.append(
                f"<{property_iri}> a owl:DatatypeProperty ;\n"
                f"    rdfs:label {_literal(name)} ; rdfs:domain <{class_iri}> ; "
                f"rdfs:range xsd:{datatype} .\n"
            )
        mappings.append(
            f"<{base}mapping/{table_segment}> a rr:TriplesMap ;\n"
            f"    rr:logicalTable [ rr:sqlQuery {_literal(query)} ] ;\n"
            f"    rr:subjectMap [ rr:template {_literal(template)} ; rr:class <{class_iri}> ] ;\n"
            + " ;\n".join(predicate_maps)
            + " .\n\n"
        )
        ontology.append("\n")
        manifest_tables.append({"target": table_name, "keys": keys, "class_iri": class_iri})

    # Validate everything before creating output files.
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    mapping_text = "".join(mappings)
    ontology_text = "".join(ontology)
    outputs = {
        "mapping": output / "mapping.ttl",
        "ontology": output / "ontology.ttl",
        "properties_example": output / "ontop.properties.example",
        "manifest": output / "manifest.json",
    }
    outputs["mapping"].write_text(mapping_text, encoding="utf-8")
    outputs["ontology"].write_text(ontology_text, encoding="utf-8")
    outputs["properties_example"].write_text(
        "# TEMPLATE ONLY. Ontop does not automatically expand these placeholders.\n"
        "# Use write_jdbc_properties_from_env() to create a private runtime file.\n"
        "# Supply the JDBC driver jar in Ontop's jdbc directory.\n"
        "jdbc.url=REPLACE_WITH_JDBC_URL\n"
        "jdbc.user=REPLACE_WITH_READ_ONLY_USER\n"
        "jdbc.password=REPLACE_WITH_RUNTIME_SECRET\n"
        "jdbc.driver=REPLACE_WITH_DRIVER_CLASS\n",
        encoding="utf-8",
    )
    manifest = {
        "format": "r2rml",
        "backend": backend,
        "approval_status": "reviewed_by_caller" if approved else "preview",
        "base_iri": base,
        "tables": manifest_tables,
        "mapping_sha256": hashlib.sha256(mapping_text.encode()).hexdigest(),
        "ontology_sha256": hashlib.sha256(ontology_text.encode()).hexdigest(),
        "execution_status": "not_executed",
        "requirements": [
            "Validate mappings using the target Ontop version and JDBC driver",
            "Verify key uniqueness and non-null constraints in the target database",
            "Review classes and properties with the business owner",
            "Use read-only credentials and restrict endpoint access",
        ],
    }
    outputs["manifest"].write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return {name: str(path) for name, path in outputs.items()}


def _property_escape(value: str) -> str:
    result: list[str] = []
    replacements = {"\\": "\\\\", "\n": "\\n", "\r": "\\r", "\t": "\\t", "=": "\\=", ":": "\\:"}
    for index, character in enumerate(value):
        if character in replacements:
            result.append(replacements[character])
        elif character == " " and index == 0:
            result.append("\\ ")
        elif ord(character) < 32 or ord(character) > 126:
            encoded = character.encode("utf-16-be")
            result.extend("\\u" + encoded[start : start + 2].hex() for start in range(0, len(encoded), 2))
        else:
            result.append(character)
    return "".join(result)


def write_jdbc_properties_from_env(
    output_path: str | Path,
    *,
    driver: str,
    jdbc_url_env: str = "ONTOP_JDBC_URL",
    user_env: str = "ONTOP_JDBC_USER",
    password_env: str = "ONTOP_JDBC_PASSWORD",
) -> str:
    """Write a new private runtime file without returning or logging secrets.

    Existing files are never overwritten. The caller must store this file outside
    version control and clean it up after use. Permission mode is 0600 on POSIX.
    """
    if not re.fullmatch(r"[A-Za-z_$][A-Za-z0-9_$.]*", driver):
        raise ValueError("driver must be a Java class name")
    values: dict[str, str] = {}
    for key, variable in (
        ("jdbc.url", jdbc_url_env),
        ("jdbc.user", user_env),
        ("jdbc.password", password_env),
    ):
        value = os.environ.get(variable)
        if value is None or (key != "jdbc.password" and not value):
            raise ValueError(f"Set the environment variable {variable}")
        values[key] = value
    if not values["jdbc.url"].startswith("jdbc:"):
        raise ValueError("The JDBC URL environment variable must start with jdbc:")
    values["jdbc.driver"] = driver
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="ascii") as stream:
        stream.write("".join(f"{key}={_property_escape(value)}\n" for key, value in values.items()))
    return str(path)
