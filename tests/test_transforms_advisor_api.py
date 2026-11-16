import json
from decimal import Decimal

import httpx
import pytest
from fastapi.testclient import TestClient

from datamigration.advisor import advise
from datamigration.api import create_app
from datamigration.demo import seed_demo
from datamigration.spec import AdvisorConfig, ColumnMapping
from datamigration.transforms import apply_column, canonical_row, coerce, decode_value, encode_value, row_key


def test_high_precision_decimal_canonicalization_is_exact():
    a = Decimal("123456789012345678901234567890123456789.123456")
    b = Decimal("123456789012345678901234567890123456789.123457")
    columns = [ColumnMapping(source="value", target="value", data_type="decimal")]
    assert canonical_row({"value": a}, columns) != canonical_row({"value": b}, columns)
    assert row_key({"value": a}, ["value"]) != row_key({"value": b}, ["value"])
    assert canonical_row({"value": Decimal("1.00")}, columns) == canonical_row({"value": Decimal("1.0")}, columns)


def test_tagged_codec_does_not_confuse_user_json_with_type_markers():
    value = {"$decimal": "ordinary text", "nested": [Decimal("2.3400"), b"\x00\xff"]}
    assert decode_value(json.loads(json.dumps(encode_value(value)))) == value


def test_lossy_conversions_fail():
    with pytest.raises(ValueError):
        coerce("1.5", "integer")
    with pytest.raises(ValueError, match="significant"):
        coerce(9007199254740993, "float")
    column = ColumnMapping(source="x", target="x", data_type="decimal", precision=5, scale=2)
    with pytest.raises(ValueError, match="precision"):
        apply_column(Decimal("1234.56"), column)
    with pytest.raises(ValueError, match="precision"):
        apply_column(Decimal("12.345"), column)


def test_advisor_uses_schema_and_requires_structured_output(monkeypatch):
    monkeypatch.setenv("MIGRATION_LLM_BASE_URL", "https://example.invalid/serving-endpoints")
    monkeypatch.setenv("MIGRATION_LLM_API_KEY", "synthetic-test-token")
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({"summary": "Review identifiers", "hypotheses": [], "questions_for_owner": ["Are identifiers stable?"], "validation_suggestions": []})}}]})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        result = advise(AdvisorConfig(provider="databricks", model="test-endpoint"), {"tables": [], "raw_samples_included": False}, client=client)
    assert result["advisory_only"]
    assert str(requests[0].url) == "https://example.invalid/serving-endpoints/chat/completions"
    assert "synthetic-test-token" not in requests[0].content.decode()


def test_api_requires_authentication_and_redacts_invalid_request(tmp_path):
    token = "a-test-token-with-more-than-24-characters"
    client = TestClient(create_app(str(tmp_path / "state"), token=token))
    assert client.get("/health").status_code == 200
    assert client.get("/runs/unknown").status_code == 401
    spec, _ = seed_demo(tmp_path / "data")
    raw = spec.model_dump()
    raw["source"]["options"] = {"password": "PRIVATE-REQUEST-VALUE"}
    response = client.post("/runs", headers={"Authorization": "Bearer " + token}, json={"spec": raw})
    assert response.status_code == 422
    assert "PRIVATE-REQUEST-VALUE" not in response.text


def test_api_authorized_run_stops_for_approval(tmp_path):
    token = "a-test-token-with-more-than-24-characters"
    client = TestClient(create_app(str(tmp_path / "state"), token=token))
    spec, _ = seed_demo(tmp_path / "data")
    headers = {"Authorization": "Bearer " + token}
    response = client.post("/runs", headers=headers, json={"spec": spec.model_dump(), "run_id": "api-run"})
    assert response.status_code == 200
    assert response.json()["status"] == "AWAITING_APPROVAL"
    decision = client.post("/runs/api-run/approval", headers=headers,
        json={"approved": True, "plan_hash": response.json()["plan_hash"], "reviewer": "api-test"})
    assert decision.status_code == 200
    assert decision.json()["validation"]["passed"]
