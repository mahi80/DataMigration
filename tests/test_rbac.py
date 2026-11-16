import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from datamigration.api import create_app
from datamigration.demo import seed_demo
from datamigration.security import (
    AuthenticationError,
    AuthorizationError,
    Principal,
    Role,
    RolePolicy,
    TokenAuthenticator,
)


TOKENS = {
    role: f"test-{role}-token-with-at-least-thirty-characters"
    for role in ("viewer", "planner", "approver", "operator", "admin")
}


def headers(role):
    return {"Authorization": "Bearer " + TOKENS[role]}


@pytest.fixture
def secured(tmp_path):
    spec, _ = seed_demo(tmp_path / "data")
    bindings = {token: {"subject": role + "-identity", "role": role} for role, token in TOKENS.items()}
    app = create_app(str(tmp_path / "state"), token_roles=bindings)
    return TestClient(app), spec, tmp_path


@pytest.mark.parametrize(
    "role,action,allowed",
    [
        ("viewer", "status", True),
        ("viewer", "discover", False),
        ("planner", "plan", True),
        ("planner", "approve", False),
        ("approver", "approve", True),
        ("approver", "plan", False),
        ("operator", "retry", True),
        ("operator", "approve", False),
        ("admin", "approve", True),
        ("admin", "unknown-action", False),
    ],
)
def test_action_policy(role, action, allowed):
    principal = Principal("identity", Role(role))
    assert RolePolicy.allows(principal, action) is allowed
    if not allowed:
        with pytest.raises(AuthorizationError):
            RolePolicy.require(principal, action)


def test_token_bindings_are_server_owned_and_use_environment_references(monkeypatch):
    monkeypatch.setenv("PLANNER_TOKEN", TOKENS["planner"])
    monkeypatch.setenv("MIGRATION_API_TOKEN", TOKENS["admin"])
    monkeypatch.setenv(
        "MIGRATION_API_TOKEN_ROLES",
        json.dumps(
            [
                {
                    "token_env": "PLANNER_TOKEN",
                    "subject": "pipeline-service",
                    "role": "planner",
                    "allowed_connections": ["SOURCE"],
                }
            ]
        ),
    )
    authenticator = TokenAuthenticator.from_config()
    principal = authenticator.authenticate(headers("planner")["Authorization"])
    assert principal.subject == "pipeline-service"
    assert principal.allowed_connections == frozenset({"SOURCE"})
    with pytest.raises(AuthenticationError):
        authenticator.authenticate(headers("admin")["Authorization"])
    with pytest.raises(AuthenticationError):
        authenticator.authenticate("Bearer wrong")
    assert TOKENS["planner"] not in repr(authenticator)


def test_discovery_requires_planner_and_contains_schemas_without_samples(secured):
    client, spec, directory = secured
    request = {"connection": spec.source.model_dump(), "scope": "source"}
    assert client.post("/discover", json=request).status_code == 401
    assert client.post("/discover", headers=headers("viewer"), json=request).status_code == 403
    result = client.post("/discover", headers=headers("planner"), json=request)
    assert result.status_code == 200
    assert result.json()["raw_samples_included"] is False
    assert "customers" in result.json()["table_names"]
    assert "Asha Verma" not in result.text
    assert "url" not in result.json()
    target = client.post(
        "/discover",
        headers=headers("planner"),
        json={"connection": spec.target.model_dump(), "scope": "target"},
    )
    assert target.status_code == 200
    assert target.json()["scope"] == "target"
    assert target.json()["table_names"] == []
    assert not (directory / "data" / "target.sqlite").exists()


def test_role_escalation_is_rejected_and_approval_records_authenticated_subject(secured):
    client, spec, directory = secured
    bad = client.post("/runs", headers=headers("planner"), json={"spec": spec.model_dump(), "role": "admin"})
    assert bad.status_code == 422
    result = client.post(
        "/runs", headers=headers("planner"), json={"spec": spec.model_dump(), "run_id": "review"}
    )
    assert result.status_code == 200
    plan_hash = result.json()["plan_hash"]
    assert client.get("/runs/review", headers=headers("viewer")).status_code == 200
    assert client.get("/runs/review/plan", headers=headers("viewer")).status_code == 403
    assert client.get("/runs/review/plan", headers=headers("approver")).status_code == 200
    decision = {"approved": True, "plan_hash": plan_hash, "reviewer": "forged-admin"}
    forged = {**headers("planner"), "X-Role": "admin", "X-Subject": "root"}
    assert client.post("/runs/review/approval", headers=forged, json=decision).status_code == 403
    assert not (directory / "data" / "target.sqlite").exists()
    accepted = client.post("/runs/review/approval", headers=headers("approver"), json=decision)
    assert accepted.status_code == 200
    with sqlite3.connect(directory / "state" / "ledger.sqlite") as database:
        assert (
            database.execute("SELECT reviewer FROM approvals WHERE run_id='review'").fetchone()[0]
            == "approver-identity"
        )


def test_scoped_discovery_filters_and_rejects_connection_table_and_option_overrides(tmp_path):
    spec, _ = seed_demo(tmp_path / "data")
    principal = Principal(
        "scoped-planner",
        Role.PLANNER,
        allowed_connections=frozenset({spec.source.url_env}),
        allowed_tables={spec.source.url_env: frozenset({"customers"})},
    )
    client = TestClient(create_app(str(tmp_path / "state"), token_roles={TOKENS["planner"]: principal}))
    request = {"connection": spec.source.model_dump()}
    response = client.post("/discover", headers=headers("planner"), json=request)
    assert response.status_code == 200
    assert response.json()["table_names"] == ["customers"]
    assert (
        client.post(
            "/discover", headers=headers("planner"), json={**request, "tables": ["orders"]}
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/discover", headers=headers("planner"), json={"connection": spec.target.model_dump()}
        ).status_code
        == 403
    )
    override = spec.source.model_dump()
    override["options"] = {"schema": "secret_schema"}
    assert (
        client.post("/discover", headers=headers("planner"), json={"connection": override}).status_code == 403
    )
    escalation = client.post(
        "/discover",
        headers=headers("planner"),
        json={**request, "allowed_tables": {spec.source.url_env: ["orders"]}},
    )
    assert escalation.status_code == 422
    assert (
        client.post("/runs", headers=headers("planner"), json={"spec": spec.model_dump()}).status_code == 403
    )


def test_approval_and_status_enforce_current_resource_scope(tmp_path):
    spec, _ = seed_demo(tmp_path / "data")
    limited = {
        "allowed_connections": [spec.source.url_env, spec.target.url_env],
        "allowed_tables": {spec.source.url_env: ["customers"], spec.target.url_env: ["customers"]},
    }
    bindings = {
        TOKENS["admin"]: {"subject": "admin", "role": "admin"},
        TOKENS["approver"]: {"subject": "restricted", "role": "approver", **limited},
        TOKENS["viewer"]: {"subject": "restricted-viewer", "role": "viewer", **limited},
    }
    client = TestClient(create_app(str(tmp_path / "state"), token_roles=bindings))
    started = client.post(
        "/runs", headers=headers("admin"), json={"spec": spec.model_dump(), "run_id": "scoped"}
    ).json()
    assert client.get("/runs/scoped", headers=headers("viewer")).status_code == 403
    assert client.get("/runs/scoped/plan", headers=headers("approver")).status_code == 403
    assert (
        client.post(
            "/runs/scoped/approval",
            headers=headers("approver"),
            json={"approved": True, "plan_hash": started["plan_hash"]},
        ).status_code
        == 403
    )
    assert not (tmp_path / "data" / "target.sqlite").exists()


def test_viewer_connector_list_and_operator_retry_gate(secured):
    client, _, _ = secured
    assert client.get("/connectors").status_code == 401
    assert "sqlite" in client.get("/connectors", headers=headers("viewer")).json()["connectors"]
    assert client.post("/runs/absent/retry", headers=headers("planner")).status_code == 403
    assert client.post("/runs/absent/retry", headers=headers("operator")).status_code == 404
