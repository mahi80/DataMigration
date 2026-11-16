import sqlite3
from datetime import datetime
from decimal import Decimal

import pytest

from datamigration.demo import seed_demo
from datamigration.maintenance import set_maintenance, status
from datamigration.quality import check_quality
from datamigration.spec import ColumnMapping, MigrationSpec, QualityRule, TableMapping
from datamigration.workflow import RunManager


def test_quality_corrects_approved_text_and_records_counts(tmp_path):
    spec, _ = seed_demo(tmp_path / "data")
    spec.tables[0].columns[2].transform = "identity"
    spec.tables[0].quality_rules = [QualityRule(name="email-format", column="email", check="regex",
        pattern=r"[^@\s]+@[^@\s]+\.[^@\s]+", correction="lower")]
    manager = RunManager(tmp_path / "state")
    state = manager.start(spec, "quality")
    result = manager.decide("quality", approved=True, plan_hash=state["plan_hash"], reviewer="owner")
    rule = result["validation"]["tables"][0]["quality_rules"]["email-format"]
    assert rule["corrected"] == 2
    assert rule["checked"] == 2


def test_quality_uniqueness_checks_final_corrected_values(tmp_path):
    spec, _ = seed_demo(tmp_path / "data")
    spec.tables[0].columns[2].transform = "identity"
    spec.tables[0].quality_rules = [
        QualityRule(name="unique-email", column="email", check="unique"),
        QualityRule(name="lower-email", column="email", check="non_blank", correction="lower"),
    ]
    with sqlite3.connect(tmp_path / "data/source.sqlite") as db:
        db.execute("UPDATE customers SET email='Same@Example.invalid' WHERE id=1")
        db.execute("UPDATE customers SET email='same@example.invalid' WHERE id=2")
    manager = RunManager(tmp_path / "state")
    state = manager.start(spec, "duplicate-quality")
    with pytest.raises(ValueError, match="unique-email"):
        manager.decide("duplicate-quality", approved=True, plan_hash=state["plan_hash"], reviewer="owner")
    assert not (tmp_path / "data/target.sqlite").exists()
    assert (manager.run_dir("duplicate-quality") / "quality_failure.json").exists()


def test_quality_range_blocks_before_write(tmp_path):
    spec, _ = seed_demo(tmp_path / "data")
    spec.tables[1].quality_rules = [QualityRule(name="nonnegative-price", column="unit_price_cents", check="range", minimum="0")]
    with sqlite3.connect(tmp_path / "data/source.sqlite") as db:
        db.execute("UPDATE products SET unit_price_cents=-1 WHERE id=10")
    manager = RunManager(tmp_path / "state")
    state = manager.start(spec, "range-quality")
    with pytest.raises(ValueError, match="nonnegative-price"):
        manager.decide("range-quality", approved=True, plan_hash=state["plan_hash"], reviewer="owner")
    assert not (tmp_path / "data/target.sqlite").exists()


def test_maintenance_blocks_new_writes_but_allows_status(tmp_path):
    spec, _ = seed_demo(tmp_path / "data")
    manager = RunManager(tmp_path / "state")
    state = manager.start(spec, "drain")
    assert set_maintenance(manager.workspace, True) == {"draining": True, "active_runs": 0}
    with pytest.raises(ValueError, match="maintenance"):
        manager.decide("drain", approved=True, plan_hash=state["plan_hash"], reviewer="owner")
    assert manager.status("drain")["status"] == "AWAITING_APPROVAL"
    set_maintenance(manager.workspace, False)
    result = manager.decide("drain", approved=True, plan_hash=state["plan_hash"], reviewer="owner")
    assert result["validation"]["passed"]
    assert status(manager.workspace)["active_runs"] == 0


@pytest.mark.parametrize("kind, first, second", [
    ("float", -0.0, 0.0),
    ("datetime", datetime.fromisoformat("2026-01-01T12:00:00+00:00"), datetime.fromisoformat("2026-01-01T17:30:00+05:30")),
    ("decimal", Decimal("1.00"), Decimal("1.0")),
])
def test_unique_rules_use_semantic_scalar_equality(kind, first, second):
    table = TableMapping(source="source", target="target", keys=["id"], columns=[
        ColumnMapping(source="id", target="id", data_type="integer"),
        ColumnMapping(source="value", target="value", data_type=kind),
    ], quality_rules=[QualityRule(name="unique-value", column="value", check="unique")])
    with sqlite3.connect(":memory:") as db:
        db.execute("CREATE TABLE quality_seen (rule TEXT, value TEXT, PRIMARY KEY(rule,value))")
        check_quality({"id": 1, "value": first}, table, db, {})
        with pytest.raises(ValueError, match="unique-value"):
            check_quality({"id": 2, "value": second}, table, db, {})


def test_allowed_values_match_declared_decimal_type():
    table = TableMapping(source="source", target="target", keys=["id"], columns=[
        ColumnMapping(source="id", target="id", data_type="integer"),
        ColumnMapping(source="rate", target="rate", data_type="decimal"),
    ], quality_rules=[QualityRule(name="approved-rates", column="rate", check="allowed_values", values=["1.00", "2.50"])])
    with sqlite3.connect(":memory:") as db:
        assert check_quality({"id": 1, "rate": Decimal("1.0")}, table, db, {})["rate"] == Decimal(1)
        with pytest.raises(ValueError, match="approved-rates"):
            check_quality({"id": 2, "rate": Decimal("1.01")}, table, db, {})


def test_signed_zero_duplicate_key_stops_before_target_creation(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    (source / "measurements.csv").write_text("id,value\n-0.0,first\n0.0,second\n")
    monkeypatch.setenv("ZERO_SOURCE", str(source))
    monkeypatch.setenv("ZERO_TARGET", f"sqlite:///{tmp_path}/target.sqlite")
    spec = MigrationSpec.model_validate({
        "name": "signed-zero-keys",
        "source": {"kind": "csv", "url_env": "ZERO_SOURCE", "options": {"schemas": {"measurements": [
            {"name": "id", "data_type": "float", "nullable": False},
            {"name": "value", "data_type": "string"},
        ]}, "keys": {"measurements": ["id"]}}},
        "target": {"kind": "sqlite", "url_env": "ZERO_TARGET"},
        "tables": [{"source": "measurements", "target": "measurements", "keys": ["id"]}],
        "policy": {"source_quiesced": True},
    })
    manager = RunManager(tmp_path / "state")
    planned = manager.start(spec, "signed-zero")
    with pytest.raises(ValueError, match="Duplicate migration keys"):
        manager.decide("signed-zero", approved=True, plan_hash=planned["plan_hash"], reviewer="owner")
    assert not (tmp_path / "target.sqlite").exists()
