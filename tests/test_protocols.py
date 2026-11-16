import asyncio
import copy
import os
from pathlib import Path
import sys

import pytest
from fastapi.testclient import TestClient

from datamigration.api import create_app
from datamigration.demo import seed_demo
from datamigration.protocols.a2a import agent_card
from datamigration.security import Principal, Role


PLANNER = "protocol-planner-token-more-than-24-characters"
APPROVER = "protocol-approver-token-more-than-24-characters"
VIEWER = "protocol-viewer-token-more-than-24-characters"


def headers(token=PLANNER, version="1.0"):
    result = {"Authorization": "Bearer " + token}
    if version is not None:
        result["A2A-Version"] = version
    return result


@pytest.fixture
def protocol(tmp_path):
    spec, _ = seed_demo(tmp_path / "data")
    app = create_app(
        str(tmp_path / "state"),
        token_roles={
            PLANNER: {"subject": "planner", "role": "planner"},
            APPROVER: {"subject": "approver", "role": "approver"},
            VIEWER: {"subject": "viewer", "role": "viewer"},
        },
        a2a_base_url="https://migration.example.invalid",
    )
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "SendMessage",
        "params": {
            "message": {
                "messageId": "client-message-1",
                "role": "ROLE_USER",
                "parts": [
                    {
                        "data": {"operation": "plan", "spec": spec.model_dump(mode="json")},
                        "mediaType": "application/json",
                    }
                ],
            }
        },
    }
    return TestClient(app), payload, tmp_path


def sdk_validate(name, value):
    sdk = pytest.importorskip("a2a.types")
    from google.protobuf.json_format import ParseDict

    return ParseDict(value, getattr(sdk, name)(), ignore_unknown_fields=False)


def test_agent_card_uses_official_v1_sdk_schema_and_cache_headers(protocol):
    client, _, _ = protocol
    response = client.get("/.well-known/agent-card.json")
    assert response.status_code == 200
    card = response.json()
    sdk_validate("AgentCard", card)
    assert card["supportedInterfaces"] == [
        {
            "url": "https://migration.example.invalid/a2a",
            "protocolBinding": "JSONRPC",
            "protocolVersion": "1.0",
        }
    ]
    assert card["capabilities"]["streaming"] is False
    assert card["securityRequirements"][0]["schemes"]["bearerAuth"] == {"list": []}
    assert (
        client.get(
            "/.well-known/agent-card.json", headers={"If-None-Match": response.headers["etag"]}
        ).status_code
        == 304
    )
    with pytest.raises(ValueError, match="HTTPS"):
        agent_card("http://public.example.invalid")


def test_a2a_requires_transport_auth_and_real_v1_method_names(protocol):
    client, payload, _ = protocol
    assert client.post("/a2a", json=payload).status_code == 401
    missing_version = client.post("/a2a", headers=headers(version=None), json=payload).json()
    assert missing_version["error"]["code"] == -32009
    assert missing_version["error"]["data"][0]["reason"] == "VERSION_NOT_SUPPORTED"
    payload["method"] = "message/send"
    assert client.post("/a2a", headers=headers(), json=payload).json()["error"]["code"] == -32601
    payload["method"] = "SendStreamingMessage"
    assert client.post("/a2a", headers=headers(), json=payload).json()["error"]["code"] == -32004
    payload["method"] = "CreateTaskPushNotificationConfig"
    assert client.post("/a2a", headers=headers(), json=payload).json()["error"]["code"] == -32003
    malformed = client.post("/a2a", headers=headers(), content="not json").json()
    assert malformed["error"]["code"] == -32700
    invalid = client.post(
        "/a2a", headers=headers(), json={"jsonrpc": "1.0", "id": True, "method": "GetTask"}
    ).json()
    assert invalid["error"]["code"] == -32600


def test_a2a_plan_requires_input_and_completes_only_after_separate_approval(protocol):
    client, payload, directory = protocol
    sdk_validate("SendMessageRequest", payload["params"])
    response = client.post("/a2a", headers=headers(), json=payload)
    assert response.status_code == 200
    result = response.json()["result"]
    sdk_validate("SendMessageResponse", result)
    task = result["task"]
    assert task["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"
    assert not (directory / "data" / "target.sqlite").exists()
    status = task["artifacts"][0]["parts"][0]["data"]
    assert status["plan_hash"]
    assert "spec" not in status
    assert "Asha Verma" not in response.text
    get = {
        "jsonrpc": "2.0",
        "id": "poll",
        "method": "GetTask",
        "params": {"id": task["id"], "historyLength": 0},
    }
    polled = client.post("/a2a", headers=headers(VIEWER), json=get).json()["result"]
    sdk_validate("Task", polled)
    assert "history" not in polled
    assert polled["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"
    approved = client.post(
        f"/runs/{task['id']}/approval",
        headers=headers(APPROVER),
        json={"approved": True, "plan_hash": status["plan_hash"], "reviewer": "untrusted-display-name"},
    )
    assert approved.status_code == 200
    complete = client.post("/a2a", headers=headers(VIEWER), json=get).json()["result"]
    assert complete["status"]["state"] == "TASK_STATE_COMPLETED"
    sdk_validate("Task", complete)


def test_a2a_rejects_role_escalation_approval_commands_and_leaking_invalid_secrets(protocol):
    client, payload, directory = protocol
    assert client.post("/a2a", headers=headers(VIEWER), json=payload).status_code == 403
    command = payload["params"]["message"]["parts"][0]["data"]
    command["role"] = "admin"
    denied = client.post("/a2a", headers=headers(), json=payload).json()
    assert denied["error"]["code"] == -32602
    del command["role"]
    command["operation"] = "approve"
    assert client.post("/a2a", headers=headers(), json=payload).json()["error"]["code"] == -32602
    command["operation"] = "plan"
    command["spec"]["source"]["options"] = {"password": "PRIVATE-A2A-INPUT"}
    denied = client.post("/a2a", headers=headers(), json=payload)
    assert denied.json()["error"]["code"] == -32602
    assert "PRIVATE-A2A-INPUT" not in denied.text
    assert not (directory / "data" / "target.sqlite").exists()


def test_a2a_nonblocking_and_message_id_deduplication_survive_app_restart(protocol):
    client, payload, directory = protocol
    payload["params"]["configuration"] = {"returnImmediately": True}
    submitted = client.post("/a2a", headers=headers(), json=payload).json()["result"]["task"]
    assert submitted["status"]["state"] == "TASK_STATE_SUBMITTED"
    repeated = client.post("/a2a", headers=headers(), json=payload).json()["result"]["task"]
    assert repeated["id"] == submitted["id"]
    assert repeated["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"
    restarted = TestClient(
        create_app(str(directory / "state"), token_roles={PLANNER: {"subject": "planner", "role": "planner"}})
    )
    poll = {"jsonrpc": "2.0", "id": 2, "method": "GetTask", "params": {"id": submitted["id"]}}
    assert (
        restarted.post("/a2a", headers=headers(), json=poll).json()["result"]["status"]["state"]
        == "TASK_STATE_INPUT_REQUIRED"
    )
    changed = copy.deepcopy(payload)
    changed["params"]["message"]["parts"][0]["data"]["spec"]["name"] = "changed-review"
    assert client.post("/a2a", headers=headers(), json=changed).json()["error"]["code"] == -32602
    assert not (directory / "data" / "target.sqlite").exists()


def test_a2a_task_not_found_and_input_required_continuation_are_explicit(protocol):
    client, payload, _ = protocol
    get = {"jsonrpc": "2.0", "id": 2, "method": "GetTask", "params": {"id": "missing"}}
    assert client.post("/a2a", headers=headers(VIEWER), json=get).json()["error"]["code"] == -32001
    task = client.post("/a2a", headers=headers(), json=payload).json()["result"]["task"]
    payload["params"]["message"]["taskId"] = task["id"]
    assert client.post("/a2a", headers=headers(), json=payload).json()["error"]["code"] == -32004


def test_a2a_resource_scope_cannot_be_overridden_in_message(protocol):
    _, payload, directory = protocol
    spec = payload["params"]["message"]["parts"][0]["data"]["spec"]
    principal = Principal("scoped", Role.PLANNER, allowed_connections=frozenset({spec["source"]["url_env"]}))
    client = TestClient(create_app(str(directory / "scoped-state"), token_roles={PLANNER: principal}))
    assert client.post("/a2a", headers=headers(), json=payload).status_code == 403


def test_mcp_tools_have_server_bound_roles_and_safe_error_output(tmp_path):
    pytest.importorskip("mcp")
    from mcp.server.fastmcp.exceptions import ToolError
    from datamigration.protocols.mcp_server import create_mcp_server

    async def run():
        viewer = create_mcp_server(str(tmp_path / "viewer"), principal=Principal("viewer", Role.VIEWER))
        listed = await viewer.list_tools()
        assert {tool.name for tool in listed} == {"list_connectors", "get_run_status"}
        assert all("role" not in tool.inputSchema["properties"] for tool in listed)
        with pytest.raises(ToolError, match="not authorized"):
            await viewer.call_tool("plan_migration", {"spec": {}})
        with pytest.raises(ToolError, match="Unexpected"):
            await viewer.call_tool("get_run_status", {"run_id": "missing", "role": "admin"})
        planner = create_mcp_server(str(tmp_path / "planner"), principal=Principal("planner", Role.PLANNER))
        with pytest.raises(ToolError) as caught:
            await planner.call_tool("plan_migration", {"spec": {"password": "PRIVATE-MCP-INPUT"}})
        assert "PRIVATE-MCP-INPUT" not in str(caught.value)

    asyncio.run(run())


def test_mcp_official_stdio_handshake_lists_and_calls_tools(tmp_path):
    pytest.importorskip("mcp")
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    async def run():
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
        environment["MIGRATION_MCP_ROLE"] = "viewer"
        server = StdioServerParameters(
            command=sys.executable,
            args=["-m", "datamigration.protocols.mcp_server", "--workspace", str(tmp_path / "stdio")],
            env=environment,
        )
        async with stdio_client(server) as (read, write):
            async with ClientSession(read, write) as session:
                initialized = await session.initialize()
                assert initialized.serverInfo.name == "DataMigration"
                listed = await session.list_tools()
                assert {tool.name for tool in listed.tools} == {"list_connectors", "get_run_status"}
                result = await session.call_tool("list_connectors", {})
                assert result.isError is False
                assert "sqlite" in result.structuredContent["connectors"]
                denied = await session.call_tool("plan_migration", {"spec": {}, "role": "admin"})
                assert denied.isError is True

    asyncio.run(run())
