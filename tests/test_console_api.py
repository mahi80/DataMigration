import json
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from datamigration.api import create_app
from datamigration.demo import seed_demo
from datamigration.spec import MigrationSpec, write_json
from datamigration.workflow import RunManager


ADMIN = "console-administrator-token-with-24-characters"
VIEWER = "console-viewer-token-with-at-least-24-characters"
PLANNER = "console-planner-token-with-at-least-24-characters"
APPROVER = "console-approver-token-with-at-least-24-characters"


def headers(token):
    return {"Authorization": f"Bearer {token}"}


def profile(identifier="source", variable="SOURCE_DB", **options):
    return {
        "id": identifier,
        "label": "Commerce source",
        "kind": "postgresql",
        "url_env": variable,
        "options": options,
    }


def mapping(source="SOURCE_DB", target="TARGET_DB", table="customers", options=None):
    return MigrationSpec.model_validate(
        {
            "name": "Commerce snapshot",
            "source": {"kind": "postgresql", "url_env": source, "options": options or {}},
            "target": {"kind": "postgresql", "url_env": target},
            "tables": [{"source": table, "target": table, "keys": ["id"]}],
            "policy": {"source_quiesced": True},
        }
    )


def record(workspace, run_id, *, spec=None, state="AWAITING_APPROVAL", created="2026-01-01T00:00:00Z"):
    path = workspace / "runs" / run_id
    path.mkdir(parents=True)
    write_json(
        path / "run.json",
        {
            "run_id": run_id,
            "status": state,
            "spec": (spec or mapping()).model_dump(mode="json"),
            "plan_hash": "a" * 64,
            "created_at": created,
            "updated_at": created,
            "error": "PRIVATE-ERROR-TEXT",
            "extra_private_field": "PRIVATE-METADATA-TEXT",
        },
    )
    return path


def test_identity_is_authenticated_and_role_is_server_bound(tmp_path):
    client = TestClient(
        create_app(
            str(tmp_path),
            token_roles={
                VIEWER: {"subject": "operations-viewer", "role": "viewer"},
            },
            connection_profiles=[],
        )
    )
    assert client.get("/me").status_code == 401
    identity = client.get("/me", headers={**headers(VIEWER), "X-Role": "admin", "X-Subject": "owner"})
    assert identity.json() == {
        "subject": "operations-viewer",
        "role": "viewer",
        "actions": ["connectors", "status"],
    }
    assert VIEWER not in identity.text
    assert client.get("/connections", headers=headers(VIEWER)).status_code == 403


def test_registry_returns_only_scoped_references_without_resolving_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("SOURCE_DB", "postgresql://private-user:PRIVATE-PASSWORD@private-host/database")
    monkeypatch.setenv(
        "MIGRATION_CONNECTIONS",
        json.dumps(
            [
                profile(),
                profile("other", "OTHER_DB"),
                profile("options_override", schema="finance"),
            ]
        ),
    )
    client = TestClient(
        create_app(
            str(tmp_path),
            token_roles={
                PLANNER: {
                    "subject": "commerce-planner",
                    "role": "planner",
                    "allowed_connections": ["SOURCE_DB"],
                    "allowed_tables": {"SOURCE_DB": ["public.customers"]},
                }
            },
        )
    )
    assert client.get("/connections").status_code == 401
    result = client.get("/connections", headers=headers(PLANNER))
    assert result.status_code == 200
    assert result.json() == {
        "connections": [
            {
                **profile(),
                "description": "",
                "allowed_tables": ["public.customers"],
            }
        ]
    }
    for private in ["PRIVATE-PASSWORD", "private-host", "OTHER_DB", "options_override"]:
        assert private not in result.text
    # Changing the environment cannot replace the already validated registry.
    monkeypatch.setenv("MIGRATION_CONNECTIONS", "not-json")
    assert client.get("/connections", headers=headers(PLANNER)).json() == result.json()


def test_approver_cannot_enumerate_connection_options(tmp_path):
    client = TestClient(
        create_app(str(tmp_path), token_roles={APPROVER: "approver"}, connection_profiles=[profile()])
    )
    assert client.get("/connections", headers=headers(APPROVER)).status_code == 403
    assert client.get("/runs", headers=headers(APPROVER)).status_code == 200


@pytest.mark.parametrize(
    "value",
    [
        "not-json",
        "{}",
        '[{"id":"one"}]',
        json.dumps([profile(), profile()]),
        json.dumps([{**profile(), "kind": "unsupported-database"}]),
        json.dumps([{**profile(), "url_env": "postgresql://user:PRIVATE-VALUE@host/database"}]),
        json.dumps([profile(password="PRIVATE-VALUE")]),
        json.dumps([{**profile(), "description": "postgresql://user:PRIVATE-VALUE@host/database"}]),
    ],
)
def test_invalid_registry_fails_startup_without_reflecting_secrets(tmp_path, monkeypatch, value):
    monkeypatch.setenv("MIGRATION_CONNECTIONS", value)
    with pytest.raises(ValueError, match="Invalid MIGRATION_CONNECTIONS") as error:
        create_app(str(tmp_path), token=ADMIN)
    assert "PRIVATE-VALUE" not in str(error.value)


def test_empty_registry_is_explicit_and_does_not_scan_environment(tmp_path, monkeypatch):
    monkeypatch.delenv("MIGRATION_CONNECTIONS", raising=False)
    monkeypatch.setenv("ARBITRARY_SECRET_DATABASE", "PRIVATE-CONNECTION-URI")
    client = TestClient(create_app(str(tmp_path), token=ADMIN))
    assert client.get("/connections", headers=headers(ADMIN)).json() == {"connections": []}


def test_run_list_scopes_before_pagination_and_does_not_hydrate_checkpoints(tmp_path, monkeypatch):
    record(tmp_path, "old", created="2026-01-01T00:00:00Z", state="COMPLETED")
    record(tmp_path, "new", created="2026-02-01T00:00:00Z", state="FAILED")
    record(tmp_path, "private", spec=mapping(source="PRIVATE_SOURCE"), created="2026-03-01T00:00:00Z")
    record(tmp_path, "private-table", spec=mapping(table="payroll"), created="2026-04-01T00:00:00Z")
    record(
        tmp_path,
        "private-options",
        spec=mapping(options={"schema": "private"}),
        created="2026-05-01T00:00:00Z",
    )
    monkeypatch.setattr(RunManager, "graph", lambda self: pytest.fail("Listing must not hydrate checkpoints"))
    client = TestClient(
        create_app(
            str(tmp_path),
            token_roles={
                VIEWER: {
                    "subject": "commerce-viewer",
                    "role": "viewer",
                    "allowed_connections": ["SOURCE_DB", "TARGET_DB"],
                    "allowed_tables": {"SOURCE_DB": ["customers"], "TARGET_DB": ["customers"]},
                }
            },
            connection_profiles=[],
        )
    )
    first = client.get("/runs?limit=1", headers=headers(VIEWER))
    assert first.status_code == 200
    data = first.json()
    assert data["total"] == 2 and data["has_more"] is True
    assert [run["run_id"] for run in data["runs"]] == ["new"]
    assert data["runs"][0]["source_connection"] == "SOURCE_DB"
    for secret in [
        "PRIVATE",
        "private-table",
        "private-options",
        "payroll",
        "spec",
        "options",
        str(tmp_path),
    ]:
        assert secret not in first.text
    second = client.get("/runs?limit=1&offset=1", headers=headers(VIEWER)).json()
    assert [run["run_id"] for run in second["runs"]] == ["old"]
    assert second["total"] == 2 and second["has_more"] is False
    filtered = client.get("/runs?status=COMPLETED", headers=headers(VIEWER)).json()
    assert filtered["total"] == 1 and filtered["runs"][0]["run_id"] == "old"


@pytest.mark.parametrize("query", ["limit=0", "limit=101", "offset=-1", "offset=5001", "status=anything"])
def test_run_inventory_request_budgets_are_validated(tmp_path, query):
    client = TestClient(create_app(str(tmp_path), token=ADMIN, connection_profiles=[]))
    assert client.get("/runs?" + query, headers=headers(ADMIN)).status_code == 422


def test_run_list_reads_only_selected_reports_and_returns_aggregates(tmp_path, monkeypatch):
    from datamigration import console

    old = record(tmp_path, "old", state="COMPLETED", created="2026-01-01T00:00:00Z")
    new = record(tmp_path, "new", state="COMPLETED", created="2026-02-01T00:00:00Z")
    for path in (old, new):
        write_json(
            path / "validation.json",
            {
                "run_id": path.name,
                "plan_hash": "a" * 64,
                "passed": True,
                "tables": [
                    {
                        "table": "customers",
                        "expected_rows": 5,
                        "target_rows": 5,
                        "missing_keys": 0,
                        "extra_keys": 0,
                        "duplicate_keys": 0,
                        "mismatched_rows": 0,
                        "quality_rules": {"trim-name": {"corrected": 3}},
                        "unexpected_private_field": "PRIVATE-REPORT-DATA",
                    }
                ],
            },
        )
    original = console._read_object
    reads = []

    def read(path, budget):
        reads.append(path)
        return original(path, budget)

    monkeypatch.setattr(console, "_read_object", read)
    client = TestClient(create_app(str(tmp_path), token=ADMIN, connection_profiles=[]))
    result = client.get("/runs?limit=1", headers=headers(ADMIN))
    assert result.json()["runs"][0]["validation"] == {
        "passed": True,
        "tables": 1,
        "expected_rows": 5,
        "target_rows": 5,
        "missing_keys": 0,
        "extra_keys": 0,
        "duplicate_keys": 0,
        "mismatched_rows": 0,
        "corrected_values": 3,
    }
    assert new / "validation.json" in reads
    assert old / "validation.json" not in reads
    assert "PRIVATE-REPORT-DATA" not in result.text


def test_legacy_timestamps_damaged_and_symlinked_records(tmp_path):
    legacy = record(tmp_path, "legacy")
    metadata = json.loads((legacy / "run.json").read_text())
    metadata.pop("created_at")
    metadata.pop("updated_at")
    write_json(legacy / "run.json", metadata)
    damaged = record(tmp_path, "damaged")
    (damaged / "run.json").write_text('{"broken":"PRIVATE-TEXT"')
    outside = tmp_path / "outside.json"
    outside.write_text('{"PRIVATE":"value"}')
    linked = tmp_path / "runs" / "linked"
    linked.mkdir()
    (linked / "run.json").symlink_to(outside)
    client = TestClient(create_app(str(tmp_path), token=ADMIN, connection_profiles=[]))
    result = client.get("/runs", headers=headers(ADMIN))
    assert result.json()["total"] == 1
    item = result.json()["runs"][0]
    assert item["run_id"] == "legacy"
    assert datetime.fromisoformat(item["created_at"]).tzinfo is not None
    assert "PRIVATE" not in result.text


def test_inventory_stops_at_pilot_capacity(tmp_path, monkeypatch):
    from datamigration import console

    monkeypatch.setattr(console, "MAX_RUNS", 1)
    record(tmp_path, "first")
    record(tmp_path, "second")
    client = TestClient(create_app(str(tmp_path), token=ADMIN, connection_profiles=[]))
    result = client.get("/runs", headers=headers(ADMIN))
    assert result.status_code == 400
    assert "pilot limit" in result.text


def test_real_console_run_retains_timestamps_approval_and_validation(tmp_path):
    spec, _ = seed_demo(tmp_path / "fixture")
    client = TestClient(
        create_app(
            str(tmp_path / "workspace"),
            token_roles={
                PLANNER: {"subject": "console-planner", "role": "planner"},
                APPROVER: {"subject": "console-owner", "role": "approver"},
            },
            connection_profiles=[],
        )
    )
    created = client.post(
        "/runs",
        headers=headers(PLANNER),
        json={"run_id": "console-run", "spec": spec.model_dump(mode="json")},
    )
    assert created.status_code == 200
    pending = created.json()
    assert pending["status"] == "AWAITING_APPROVAL"
    assert datetime.fromisoformat(pending["created_at"]).tzinfo is not None
    denied = client.post(
        "/runs/console-run/approval",
        headers=headers(PLANNER),
        json={"approved": True, "plan_hash": pending["plan_hash"]},
    )
    assert denied.status_code == 403
    approved = client.post(
        "/runs/console-run/approval",
        headers=headers(APPROVER),
        json={"approved": True, "plan_hash": pending["plan_hash"], "reviewer": "spoofed-user"},
    )
    assert approved.status_code == 200
    assert approved.json()["status"] == "COMPLETED"
    assert approved.json()["created_at"] == pending["created_at"]
    assert approved.json()["updated_at"] >= pending["updated_at"]
    listed = client.get("/runs", headers=headers(APPROVER)).json()
    assert listed["total"] == 1
    assert listed["runs"][0]["validation"]["passed"] is True
    assert listed["runs"][0]["validation"]["expected_rows"] == 9
    assert listed["runs"][0]["validation"]["target_rows"] == 9
    assert "artifact_directory" not in listed["runs"][0]


def test_running_retry_does_not_surface_an_old_validation_result(tmp_path):
    run = record(tmp_path, "running", state="RUNNING")
    write_json(
        run / "validation.json", {"run_id": "running", "plan_hash": "a" * 64, "passed": True, "tables": []}
    )
    client = TestClient(create_app(str(tmp_path), token=ADMIN, connection_profiles=[]))
    result = client.get("/runs", headers=headers(ADMIN)).json()
    assert "validation" not in result["runs"][0]
