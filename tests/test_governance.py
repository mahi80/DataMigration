from __future__ import annotations

import json
import os
import stat
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from rdflib import Graph, Namespace, RDF, URIRef

from datamigration.governance import (
    OpenMetadataClient,
    OpenMetadataError,
    build_lineage_payload,
    build_table_payload,
    export_ontop,
    write_jdbc_properties_from_env,
)


@pytest.fixture
def catalog():
    return {
        "tables": [
            {
                "name": "customer",
                "primary_key": ["company_id", "customer_id"],
                "columns": [
                    {"name": "company_id", "data_type": "integer", "nullable": False},
                    {"name": "customer_id", "data_type": "string", "nullable": False},
                    {"name": "name", "data_type": "string", "nullable": True},
                ],
            }
        ],
    }


@pytest.fixture
def mapping():
    return {
        "target": {"kind": "postgresql", "url_env": "TARGET_DATABASE_URL"},
        "tables": [
            {
                "source": "customer",
                "target": 'public.Customer "Master"',
                "keys": ["company{id}", "customer id"],
                "columns": [
                    {
                        "source": "company_id",
                        "target": "company{id}",
                        "data_type": "integer",
                        "nullable": False,
                    },
                    {
                        "source": "customer_id",
                        "target": "customer id",
                        "data_type": "string",
                        "nullable": False,
                    },
                    {"source": "name", "target": 'display"name', "data_type": "string", "nullable": True},
                ],
            }
        ],
    }


def test_catalog_composite_key_is_one_constraint(catalog):
    payload = build_table_payload(catalog["tables"][0], "prod.commerce.public")
    assert payload["tableConstraints"] == [
        {"constraintType": "PRIMARY_KEY", "columns": ["company_id", "customer_id"]}
    ]
    assert [column["constraint"] for column in payload["columns"]] == ["NOT_NULL", "NOT_NULL", "NULL"]
    broken = deepcopy(catalog["tables"][0])
    broken["primary_key"] = ["absent"]
    with pytest.raises(ValueError, match="existing columns"):
        build_table_payload(broken, "prod.commerce.public")


def test_unknown_catalog_type_is_never_silently_published(catalog):
    table = catalog["tables"][0]
    table["columns"][0]["data_type"] = "vector_or_custom_udt"
    with pytest.raises(ValueError, match="Unsupported normalized"):
        build_table_payload(table, "prod.commerce.public")


def test_catalog_excludes_samples_and_rejects_server_invalid_column_names(catalog):
    table = catalog["tables"][0]
    table["sampleData"] = {"rows": [["private customer value"]]}
    table["connection_url"] = "postgresql://secret@example/db"
    payload = build_table_payload(table, "prod.commerce.public")
    assert "private customer value" not in json.dumps(payload)
    assert "secret@example" not in json.dumps(payload)
    table["columns"][0]["name"] = 'bad"name'
    with pytest.raises(ValueError, match="OpenMetadata column names"):
        build_table_payload(table, "prod.commerce.public")


@pytest.mark.parametrize("suffix", ["", "/api", "/api/v1", "/api/"])
def test_publication_is_explicit_and_preserves_api_prefix(monkeypatch, catalog, suffix):
    monkeypatch.setenv("OM_TEST_TOKEN", "test-only-bearer")
    observed = []

    def respond(request):
        observed.append(request)
        assert request.headers["Authorization"] == "Bearer test-only-bearer"
        if request.method == "GET":
            assert request.url.path == "/api/v1/databaseSchemas/name/prod.commerce.public"
            return httpx.Response(200, json={"id": str(uuid4())})
        assert request.url.path == "/api/v1/tables"
        assert json.loads(request.content)["databaseSchema"] == "prod.commerce.public"
        return httpx.Response(
            200, json={"id": str(uuid4()), "fullyQualifiedName": "prod.commerce.public.customer"}
        )

    with OpenMetadataClient(
        "https://metadata.example" + suffix,
        token_env="OM_TEST_TOKEN",
        transport=httpx.MockTransport(respond),
    ) as client:
        with pytest.raises(PermissionError):
            client.publish_catalog(catalog, "prod.commerce.public")
        assert observed == []
        assert len(client.publish_catalog(catalog, "prod.commerce.public", approved=True)) == 1
    assert [request.method for request in observed] == ["GET", "PUT"]


def test_invalid_later_table_prevents_any_partial_publish(monkeypatch, catalog):
    monkeypatch.setenv("OPENMETADATA_TOKEN", "not-a-real-token")
    calls = []
    catalog["tables"].append({"name": "broken", "columns": []})
    with OpenMetadataClient(
        "https://metadata.example",
        transport=httpx.MockTransport(lambda request: calls.append(request)),
    ) as client:
        with pytest.raises(ValueError, match="at least one"):
            client.publish_catalog(catalog, "prod.commerce.public", approved=True)
    assert calls == []


def test_lineage_uses_server_fqns_and_empty_success_body(monkeypatch):
    source = {
        "id": str(uuid4()),
        "fullyQualifiedName": "src.db.public.customer",
        "columns": [{"name": "source.id", "fullyQualifiedName": 'src.db.public.customer."source.id"'}],
    }
    target = {
        "id": str(uuid4()),
        "fullyQualifiedName": "dst.db.public.dim_customer",
        "columns": [{"name": "customer_id"}],
    }
    payload = build_lineage_payload(source, target, [{"source": "source.id", "target": "customer_id"}])
    assert payload["edge"]["lineageDetails"]["columnsLineage"][0] == {
        "fromColumns": ['src.db.public.customer."source.id"'],
        "toColumn": "dst.db.public.dim_customer.customer_id",
    }
    monkeypatch.setenv("OPENMETADATA_TOKEN", "test-bearer")
    seen = []

    def respond(request):
        seen.append(request)
        return httpx.Response(200)

    with OpenMetadataClient("https://metadata.example/api", transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(PermissionError):
            client.add_lineage(payload)
        assert client.add_lineage(payload, approved=True) == {}
    assert len(seen) == 1
    assert seen[0].url.path == "/api/v1/lineage"
    with pytest.raises(ValueError, match="absent"):
        build_lineage_payload(source, target, [{"source": "unknown", "target": "customer_id"}])


def test_catalog_failure_is_visible_and_does_not_leak_token(monkeypatch, catalog):
    monkeypatch.setenv("OPENMETADATA_TOKEN", "secret-must-not-be-logged")
    with OpenMetadataClient(
        "https://metadata.example",
        transport=httpx.MockTransport(lambda request: httpx.Response(403, text="private backend details")),
    ) as client:
        with pytest.raises(OpenMetadataError) as error:
            client.publish_catalog(catalog, "prod.commerce.public", approved=True)
    assert "403" in str(error.value)
    assert "secret-must-not-be-logged" not in str(error.value)
    assert "private backend details" not in str(error.value)


def test_r2rml_escapes_identifiers_and_emits_distinct_composite_key_terms(tmp_path, mapping):
    paths = export_ontop(mapping, tmp_path)
    graph = Graph().parse(paths["mapping"], format="turtle")
    ontology = Graph().parse(paths["ontology"], format="turtle")
    rr = Namespace("http://www.w3.org/ns/r2rml#")
    owl = Namespace("http://www.w3.org/2002/07/owl#")
    triples_maps = list(graph.subjects(RDF.type, rr.TriplesMap))
    assert len(triples_maps) == 1
    logical_table = graph.value(triples_maps[0], rr.logicalTable)
    sql = str(graph.value(logical_table, rr.sqlQuery))
    assert 'FROM "public"."Customer ""Master"""' in sql
    assert '"display""name" AS dm_col_2' in sql
    subject = graph.value(triples_maps[0], rr.subjectMap)
    template = str(graph.value(subject, rr.template))
    assert template.endswith("/{dm_col_0}/{dm_col_1}")
    assert "%22Master%22" in template
    assert "company{id}" not in template
    assert len(list(ontology.subjects(RDF.type, owl.Class))) == 1
    assert len(list(ontology.subjects(RDF.type, owl.DatatypeProperty))) == 3
    manifest = json.loads(Path(paths["manifest"]).read_text())
    assert manifest["approval_status"] == "preview"
    assert manifest["execution_status"] == "not_executed"
    assert len(manifest["mapping_sha256"]) == 64


def test_r2rml_is_deterministic_and_export_contains_no_connection_secrets(tmp_path, mapping, monkeypatch):
    monkeypatch.setenv("TARGET_DATABASE_URL", "postgresql://user:should-not-appear@db.example/private")
    first = export_ontop(mapping, tmp_path / "a", approved=True)
    second = export_ontop(mapping, tmp_path / "b", approved=True)
    for key in first:
        assert Path(first[key]).read_bytes() == Path(second[key]).read_bytes()
        assert "should-not-appear" not in Path(first[key]).read_text()


@pytest.mark.parametrize("backend", ["redis", "mongodb", "cassandra", "delta", "sqlite"])
def test_ontop_rejects_unadvertised_native_backends_before_writing(tmp_path, mapping, backend):
    mapping["target"]["kind"] = backend
    with pytest.raises(ValueError, match="SQL-facing"):
        export_ontop(mapping, tmp_path / "output")
    assert not (tmp_path / "output").exists()


def test_ontop_requires_keys_and_explicit_scalar_representation(tmp_path, mapping):
    invalid = deepcopy(mapping)
    invalid["tables"][0]["keys"] = []
    with pytest.raises(ValueError, match="keys"):
        export_ontop(invalid, tmp_path)
    invalid = deepcopy(mapping)
    invalid["tables"][0]["columns"][0]["nullable"] = True
    with pytest.raises(ValueError, match="non-null"):
        export_ontop(invalid, tmp_path)
    invalid = deepcopy(mapping)
    invalid["tables"][0]["columns"][2]["data_type"] = "json"
    with pytest.raises(ValueError, match="scalar representation"):
        export_ontop(invalid, tmp_path)


def test_jdbc_runtime_file_is_private_escaped_and_never_overwritten(tmp_path, monkeypatch):
    monkeypatch.setenv("ONTOP_JDBC_URL", "jdbc:postgresql://localhost:5432/example")
    monkeypatch.setenv("ONTOP_JDBC_USER", "reader")
    monkeypatch.setenv("ONTOP_JDBC_PASSWORD", " leading:secret=हिन्दी\nnot-a-new-property")
    path = tmp_path / "private" / "ontop.properties"
    result = write_jdbc_properties_from_env(path, driver="org.postgresql.Driver")
    assert result == str(path)
    text = path.read_text()
    assert "jdbc\\:postgresql\\://" in text
    assert "jdbc.password=\\ leading\\:secret\\=" in text
    assert "\\nnot-a-new-property" in text
    assert "हिन्दी" not in text
    assert len(text.splitlines()) == 4
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    with pytest.raises(FileExistsError):
        write_jdbc_properties_from_env(path, driver="org.postgresql.Driver")


def test_databricks_quotes_catalog_schema_and_table(tmp_path, mapping):
    mapping["target"]["kind"] = "databricks"
    mapping["tables"][0]["target"] = "commerce.gold.Customer Master"
    paths = export_ontop(mapping, tmp_path)
    graph = Graph().parse(paths["mapping"], format="turtle")
    query = next(graph.objects(None, URIRef("http://www.w3.org/ns/r2rml#sqlQuery")))
    assert "FROM `commerce`.`gold`.`Customer Master`" in str(query)
