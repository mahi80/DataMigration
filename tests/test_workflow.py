import json
import sqlite3

import pytest

from datamigration.connectors.sql import SQLConnector
from datamigration.demo import seed_demo
from datamigration.engine import build_plan, discover
from datamigration.spec import MigrationSpec
from datamigration.workflow import RunManager, safe_error


@pytest.fixture
def prepared(tmp_path):
    spec, _ = seed_demo(tmp_path / "data")
    return spec, RunManager(tmp_path / "state"), tmp_path / "data"


def approve(manager, state):
    return manager.decide(state["run_id"], approved=True, plan_hash=state["plan_hash"], reviewer="test-operator")


def test_approval_survives_process_restart_and_reconciles(prepared):
    spec, manager, data = prepared
    state = manager.start(spec, "restart")
    assert state["status"] == "AWAITING_APPROVAL"
    assert not (data / "target.sqlite").exists()
    restarted = RunManager(manager.workspace)
    result = approve(restarted, state)
    assert result["status"] == "COMPLETED"
    assert result["validation"]["passed"]
    with sqlite3.connect(data / "target.sqlite") as db:
        assert db.execute("SELECT full_name,email FROM customers WHERE customer_id=1").fetchone() == ("Asha Verma", "asha@example.invalid")


def test_bad_approval_hash_never_writes(prepared):
    spec, manager, data = prepared
    state = manager.start(spec, "bad-hash")
    with pytest.raises(ValueError, match="hash"):
        manager.decide(state["run_id"], approved=True, plan_hash="0" * 64, reviewer="operator")
    assert manager.status("bad-hash")["status"] == "AWAITING_APPROVAL"
    assert not (data / "target.sqlite").exists()


def test_rejection_is_terminal_without_writes(prepared):
    spec, manager, data = prepared
    state = manager.start(spec, "reject")
    result = manager.decide("reject", approved=False, plan_hash=state["plan_hash"], reviewer="operator")
    assert result["status"] == "REJECTED"
    assert not (data / "target.sqlite").exists()


def test_replay_after_write_acknowledgement_loss_is_idempotent(prepared, monkeypatch):
    spec, manager, data = prepared
    state = manager.start(spec, "replay")
    original = SQLConnector.write_batch
    calls = []

    def fail_after_commit(self, *args, **kwargs):
        count = original(self, *args, **kwargs)
        calls.append(1)
        if len(calls) == 1:
            raise ConnectionError("Simulated lost acknowledgement after committed write")
        return count

    monkeypatch.setattr(SQLConnector, "write_batch", fail_after_commit)
    with pytest.raises(ConnectionError):
        approve(manager, state)
    result = RunManager(manager.workspace).retry("replay")
    assert result["validation"]["passed"]
    with sqlite3.connect(data / "target.sqlite") as db:
        assert db.execute("SELECT COUNT(*) FROM customers").fetchone()[0] == 2


def test_source_schema_drift_stops_before_target_writes(prepared):
    spec, manager, data = prepared
    state = manager.start(spec, "drift")
    with sqlite3.connect(data / "source.sqlite") as db:
        db.execute("ALTER TABLE customers ADD COLUMN unexpected TEXT")
    with pytest.raises(ValueError, match="schema changed"):
        approve(manager, state)
    assert not (data / "target.sqlite").exists()


def test_changed_destination_environment_invalidates_approval(prepared, monkeypatch):
    spec, manager, data = prepared
    state = manager.start(spec, "endpoint")
    other = data / "other.sqlite"
    monkeypatch.setenv(spec.target.url_env, f"sqlite:///{other}")
    with pytest.raises(ValueError, match="Connection environment changed"):
        approve(manager, state)
    assert not other.exists()


def test_all_sources_staged_before_any_target_writes(prepared):
    spec, manager, data = prepared
    spec.tables[2].keys = ["customer_id"]
    with sqlite3.connect(data / "source.sqlite") as db:
        db.execute("INSERT INTO orders VALUES(300,1,'2026-11-03T10:00:00')")
    state = manager.start(spec, "duplicate")
    with pytest.raises(ValueError, match="Duplicate migration keys"):
        approve(manager, state)
    assert not (data / "target.sqlite").exists()


def test_source_quiescence_is_explicit(prepared):
    spec, manager, data = prepared
    spec.policy.source_quiesced = False
    with pytest.raises(ValueError, match="source_quiesced"):
        manager.start(spec, "live-source")
    assert not (data / "target.sqlite").exists()


def test_tampered_plan_cannot_be_approved(prepared):
    spec, manager, data = prepared
    state = manager.start(spec, "tamper")
    path = manager.run_dir("tamper") / "plan.json"
    plan = json.loads(path.read_text())
    plan["spec"]["tables"][0]["columns"][0]["target"] = "different_id"
    path.write_text(json.dumps(plan))
    with pytest.raises(ValueError, match="integrity"):
        approve(manager, state)
    assert not (data / "target.sqlite").exists()


def test_relative_endpoint_rebinding_is_detected(tmp_path, monkeypatch):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    for directory in (a, b):
        with sqlite3.connect(directory / "source.sqlite") as db:
            db.execute("CREATE TABLE items(id INTEGER PRIMARY KEY)")
            db.execute("INSERT INTO items VALUES(1)")
    monkeypatch.setenv("TEST_SOURCE", "sqlite:///source.sqlite")
    monkeypatch.setenv("TEST_TARGET", "sqlite:///target.sqlite")
    spec = MigrationSpec.model_validate({"name": "relative", "source": {"kind": "sqlite", "url_env": "TEST_SOURCE"},
        "target": {"kind": "sqlite", "url_env": "TEST_TARGET"}, "tables": [{"source": "items", "target": "items", "keys": ["id"]}], "policy": {"source_quiesced": True}})
    monkeypatch.chdir(a)
    manager = RunManager(tmp_path / "state")
    state = manager.start(spec, "relative")
    monkeypatch.chdir(b)
    with pytest.raises(ValueError, match="Connection environment changed"):
        approve(manager, state)
    assert not (b / "target.sqlite").exists()


def test_in_memory_and_same_resource_are_rejected(prepared, monkeypatch):
    spec, _, data = prepared
    monkeypatch.setenv(spec.target.url_env, "sqlite:///:memory:")
    with pytest.raises(ValueError, match="durable SQLite"):
        spec.target.resolve()
    monkeypatch.setenv(spec.target.url_env, f"sqlite+pysqlite:///{data / 'source.sqlite'}")
    with pytest.raises(ValueError, match="distinct source"):
        build_plan(spec, discover(spec))


def test_target_corruption_is_detected(prepared, monkeypatch):
    spec, manager, _ = prepared
    state = manager.start(spec, "corrupt")
    original = SQLConnector.write_batch

    def corrupt(self, table, rows, keys):
        if table == "customers":
            rows = [dict(row, full_name="unexpected-value") for row in rows]
        return original(self, table, rows, keys)

    monkeypatch.setattr(SQLConnector, "write_batch", corrupt)
    with pytest.raises(ValueError, match="Reconciliation failed"):
        approve(manager, state)
    report = manager.status("corrupt")["validation"]
    assert report["passed"] is False
    assert report["tables"][0]["mismatched_rows"] == 2


def test_secret_validation_does_not_echo_credentials(prepared):
    spec, _, _ = prepared
    raw = spec.model_dump()
    raw["source"]["options"] = {"engine_options": {"connect_args": {"password": "VERY_PRIVATE_VALUE"}}}
    with pytest.raises(ValueError) as error:
        MigrationSpec.model_validate(raw)
    assert "VERY_PRIVATE_VALUE" not in safe_error(error.value)


def test_explicit_star_schema_to_delta(tmp_path):
    spec, _ = seed_demo(tmp_path / "star", star=True, delta=True)
    manager = RunManager(tmp_path / "state")
    result = approve(manager, manager.start(spec, "star"))
    assert result["validation"]["passed"]
    assert {t["table"]: t["expected_rows"] for t in result["validation"]["tables"]} == {"dim_customer": 2, "dim_product": 2, "fact_sales": 3}
