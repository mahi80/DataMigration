"""A2A 1.0 JSON-RPC binding for migration planning and task polling.

Wire names, enums, Agent Card security requirements, and error codes follow
the official 1.0 specification/proto. The JSON in a message's data part is an
application-level planning command, not a replacement for the A2A envelope.
"""

from __future__ import annotations

import copy
import json
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any, Callable, Literal
from urllib.parse import urlsplit
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from ..security import Principal, RolePolicy
from ..spec import MigrationSpec, fingerprint
from ..workflow import RunManager, safe_error
from .service import MigrationService, public_status


PROTOCOL_VERSION = "1.0"
_TERMINAL = {"TASK_STATE_COMPLETED", "TASK_STATE_FAILED", "TASK_STATE_CANCELED", "TASK_STATE_REJECTED"}
_STATES = {
    "CREATED": "TASK_STATE_SUBMITTED",
    "RUNNING": "TASK_STATE_WORKING",
    "DISCOVERED": "TASK_STATE_WORKING",
    "CONTEXT_REVIEW": "TASK_STATE_WORKING",
    "AWAITING_APPROVAL": "TASK_STATE_INPUT_REQUIRED",
    "APPROVED": "TASK_STATE_WORKING",
    "VALIDATED": "TASK_STATE_WORKING",
    "COMPLETED": "TASK_STATE_COMPLETED",
    "FAILED": "TASK_STATE_FAILED",
    "REJECTED": "TASK_STATE_REJECTED",
}


class WireModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Part(WireModel):
    text: str | None = None
    raw: str | None = None
    url: str | None = None
    data: Any = None
    metadata: dict[str, Any] | None = None
    filename: str | None = None
    mediaType: str | None = None

    @model_validator(mode="after")
    def one_content(self):
        count = sum(name in self.model_fields_set for name in ("text", "raw", "url", "data"))
        if count != 1:
            raise ValueError("An A2A part requires exactly one content member")
        return self


class ClientMessage(WireModel):
    messageId: str = Field(min_length=1, max_length=200)
    role: Literal["ROLE_USER"]
    parts: list[Part] = Field(min_length=1, max_length=10)
    contextId: str | None = Field(default=None, min_length=1, max_length=200)
    taskId: str | None = Field(default=None, min_length=1, max_length=200)
    metadata: dict[str, Any] | None = None
    extensions: list[str] | None = None
    referenceTaskIds: list[str] | None = None


class SendConfiguration(WireModel):
    acceptedOutputModes: list[str] | None = None
    taskPushNotificationConfig: dict[str, Any] | None = None
    historyLength: int | None = Field(default=None, ge=0)
    returnImmediately: bool = False


class SendMessageRequest(WireModel):
    message: ClientMessage
    configuration: SendConfiguration = Field(default_factory=SendConfiguration)
    metadata: dict[str, Any] | None = None
    tenant: str | None = None


class GetTaskRequest(WireModel):
    id: str = Field(min_length=1, max_length=200)
    historyLength: int | None = Field(default=None, ge=0)
    tenant: str | None = None


class PlanCommand(WireModel):
    operation: Literal["plan"]
    spec: MigrationSpec


class RPCRequest(WireModel):
    jsonrpc: Literal["2.0"]
    id: str | int | None = None
    method: str = Field(min_length=1)
    params: dict[str, Any] = Field(default_factory=dict)


class RPCError(Exception):
    def __init__(self, code: int, message: str, reason: str | None = None):
        super().__init__(message)
        self.code, self.message, self.reason = code, message, reason


def rpc_error(request_id: Any, code: int, message: str, reason: str | None = None) -> dict:
    error: dict[str, Any] = {"code": code, "message": message}
    if reason:
        error["data"] = [
            {
                "@type": "type.googleapis.com/google.rpc.ErrorInfo",
                "reason": reason,
                "domain": "a2a-protocol.org",
            }
        ]
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _base_url(value: str) -> str:
    parsed = urlsplit(value)
    local = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    if not parsed.netloc or (parsed.scheme != "https" and not (parsed.scheme == "http" and local)):
        raise ValueError("A2A public URL requires HTTPS, except for loopback development")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("A2A public URL cannot contain credentials, a query, or a fragment")
    return value.rstrip("/")


def agent_card(base_url: str) -> dict:
    base = _base_url(base_url)
    return {
        "name": "DataMigration Planning Agent",
        "description": "Prepare hash-bound migration plans and poll task status. Destination writes require a separate authenticated approval API decision.",
        "version": "0.1.0",
        "supportedInterfaces": [
            {"url": base + "/a2a", "protocolBinding": "JSONRPC", "protocolVersion": PROTOCOL_VERSION}
        ],
        "capabilities": {"streaming": False, "pushNotifications": False, "extendedAgentCard": False},
        "securitySchemes": {"bearerAuth": {"httpAuthSecurityScheme": {"scheme": "Bearer"}}},
        # SecurityRequirement / StringList are nested proto messages in 1.0.
        "securityRequirements": [{"schemes": {"bearerAuth": {"list": []}}}],
        "defaultInputModes": ["application/json"],
        "defaultOutputModes": ["application/json"],
        "skills": [
            {
                "id": "plan-migration",
                "name": "Prepare a migration for approval",
                "description": "Send one data part containing {operation: plan, spec: MigrationSpec}. The task becomes INPUT_REQUIRED for independent approval; GetTask polls the outcome. No approvals, arbitrary SQL, or stored-procedure execution are accepted over A2A.",
                "tags": ["data-migration", "planning", "approval"],
            }
        ],
    }


class A2AService:
    """Durable local task identity with blocking or background plan creation.

    Nonblocking execution uses the hosting app's background task runner, not a
    distributed queue. An interrupted local worker is recovered through the
    core operator retry workflow or a newly submitted planning task.
    """

    def __init__(self, manager: RunManager, base_url: str):
        self.manager = manager
        self.base_url = _base_url(base_url)
        self.path = manager.workspace / "a2a-tasks.sqlite"
        with sqlite3.connect(self.path, timeout=30) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("""CREATE TABLE IF NOT EXISTS a2a_tasks (
                id TEXT PRIMARY KEY, subject TEXT NOT NULL, message_id TEXT NOT NULL,
                request_hash TEXT NOT NULL, body TEXT NOT NULL, scope TEXT NOT NULL,
                UNIQUE(subject,message_id))""")

    def _load(self, task_id: str) -> dict:
        with sqlite3.connect(self.path, timeout=30) as db:
            row = db.execute("SELECT body FROM a2a_tasks WHERE id=?", (task_id,)).fetchone()
        if not row:
            raise RPCError(-32001, "Task not found", "TASK_NOT_FOUND")
        return json.loads(row[0])

    def _store(self, task: dict) -> dict:
        with sqlite3.connect(self.path, timeout=30) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT body FROM a2a_tasks WHERE id=?", (task["id"],)).fetchone()
            if row:
                previous = json.loads(row[0])
                if previous["status"]["state"] in _TERMINAL:
                    return previous
            db.execute("UPDATE a2a_tasks SET body=? WHERE id=?", (json.dumps(task), task["id"]))
        return task

    @staticmethod
    def _message(task: dict, text: str) -> dict:
        return {
            "messageId": str(uuid4()),
            "role": "ROLE_AGENT",
            "contextId": task["contextId"],
            "taskId": task["id"],
            "parts": [{"text": text, "mediaType": "text/plain"}],
        }

    def _refresh(self, task_id: str) -> dict:
        task = self._load(task_id)
        if task["status"]["state"] in _TERMINAL:
            return task
        try:
            status = public_status(self.manager.status(task_id))
        except FileNotFoundError:
            return task
        phase = status["status"]
        state = _STATES.get(phase, "TASK_STATE_UNSPECIFIED")
        if state == "TASK_STATE_INPUT_REQUIRED":
            message = "Review the exact plan using the plan URL. A separately authenticated approver must decide through the approval API. A2A cannot approve destination writes."
        elif state == "TASK_STATE_FAILED":
            message = status.get("error", "Migration planning or execution failed")
        else:
            message = f"Migration lifecycle: {phase}"
        timestamp = task["status"]["timestamp"] if task["status"]["state"] == state else _timestamp()
        task["status"] = {"state": state, "timestamp": timestamp, "message": self._message(task, message)}
        result = {"run_id": task_id, "status": phase, "plan_hash": status.get("plan_hash")}
        if status.get("plan_hash"):
            result["plan_url"] = self.base_url + f"/runs/{task_id}/plan"
            result["approval_url"] = self.base_url + f"/runs/{task_id}/approval"
        if status.get("validation"):
            result["validation"] = status["validation"]
        task["artifacts"] = [
            {
                "artifactId": task_id + "-status",
                "name": "migration-status",
                "parts": [{"data": result, "mediaType": "application/json"}],
            }
        ]
        return self._store(task)

    def get_task(self, principal: Principal, params: GetTaskRequest) -> dict:
        RolePolicy.require(principal, "status")
        if params.tenant:
            raise RPCError(-32602, "This deployment has no tenant routing")
        with sqlite3.connect(self.path, timeout=30) as db:
            row = db.execute("SELECT scope FROM a2a_tasks WHERE id=?", (params.id,)).fetchone()
        if not row:
            raise RPCError(-32001, "Task not found", "TASK_NOT_FOUND")
        RolePolicy.spec(principal, MigrationSpec.model_validate_json(row[0]))
        task = copy.deepcopy(self._refresh(params.id))
        # This adapter deliberately retains no input-message/spec history.
        task.pop("history", None)
        return task

    def _plan(self, task_id: str, principal: Principal, spec: MigrationSpec) -> None:
        try:
            MigrationService(self.manager, principal).start(spec, task_id)
            self._refresh(task_id)
        except Exception as exc:
            task = self._load(task_id)
            task["status"] = {
                "state": "TASK_STATE_FAILED",
                "timestamp": _timestamp(),
                "message": self._message(task, safe_error(exc, spec.model_dump())),
            }
            self._store(task)

    def send_message(
        self,
        principal: Principal,
        params: SendMessageRequest,
        schedule: Callable[..., None] | None = None,
    ) -> dict:
        RolePolicy.require(principal, "plan")
        if params.tenant:
            raise RPCError(-32602, "This deployment has no tenant routing")
        configuration, message = params.configuration, params.message
        if configuration.taskPushNotificationConfig is not None:
            raise RPCError(-32003, "Push notifications are not supported", "PUSH_NOTIFICATION_NOT_SUPPORTED")
        if (
            configuration.acceptedOutputModes is not None
            and "application/json" not in configuration.acceptedOutputModes
        ):
            raise RPCError(
                -32005, "This skill produces application/json artifacts", "CONTENT_TYPE_NOT_SUPPORTED"
            )
        if message.taskId:
            existing = self.get_task(principal, GetTaskRequest(id=message.taskId))
            if message.contextId and message.contextId != existing["contextId"]:
                raise RPCError(-32602, "The supplied context does not match this task")
            raise RPCError(
                -32004, "Use GetTask to poll and the separate approval API to decide", "UNSUPPORTED_OPERATION"
            )
        if len(message.parts) != 1 or "data" not in message.parts[0].model_fields_set:
            raise RPCError(
                -32005, "This skill accepts one application/json data part", "CONTENT_TYPE_NOT_SUPPORTED"
            )
        if message.parts[0].mediaType not in (None, "application/json"):
            raise RPCError(-32005, "This skill accepts application/json", "CONTENT_TYPE_NOT_SUPPORTED")
        command = PlanCommand.model_validate(message.parts[0].data)
        RolePolicy.spec(principal, command.spec)
        if configuration.returnImmediately and schedule is None:
            raise RPCError(
                -32004, "A background runner is required for nonblocking requests", "UNSUPPORTED_OPERATION"
            )
        request_hash = fingerprint(
            {"command": command.model_dump(mode="json"), "contextId": message.contextId}
        )
        task_id = "a2a-" + uuid4().hex
        task = {
            "id": task_id,
            "contextId": message.contextId or str(uuid4()),
            "status": {"state": "TASK_STATE_SUBMITTED", "timestamp": _timestamp()},
            "metadata": {"runId": task_id},
        }
        with sqlite3.connect(self.path, timeout=30) as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute(
                "SELECT id,request_hash FROM a2a_tasks WHERE subject=? AND message_id=?",
                (principal.subject, message.messageId),
            ).fetchone()
            if previous:
                if previous[1] != request_hash:
                    raise RPCError(-32602, "A messageId cannot be reused with changed planning content")
                task_id = previous[0]
            else:
                db.execute(
                    "INSERT INTO a2a_tasks VALUES(?,?,?,?,?,?)",
                    (
                        task_id,
                        principal.subject,
                        message.messageId,
                        request_hash,
                        json.dumps(task),
                        command.spec.model_dump_json(),
                    ),
                )
        if not previous:
            if configuration.returnImmediately:
                schedule(self._plan, task_id, principal, command.spec)
                return {"task": task}
            self._plan(task_id, principal, command.spec)
        return {
            "task": self.get_task(
                principal, GetTaskRequest(id=task_id, historyLength=configuration.historyLength)
            )
        }

    def dispatch(
        self,
        payload: Any,
        principal: Principal,
        *,
        version: str,
        schedule: Callable[..., None] | None = None,
    ) -> dict:
        try:
            request = RPCRequest.model_validate(payload)
        except ValidationError:
            return rpc_error(None, -32600, "Request payload validation error")
        try:
            # An omitted header means 0.3 in A2A; never silently treat it as 1.0.
            if not re.fullmatch(r"1\.0(?:\.\d+)?", version or "0.3"):
                raise RPCError(-32009, "Use A2A-Version: 1.0", "VERSION_NOT_SUPPORTED")
            if request.method == "SendMessage":
                result = self.send_message(
                    principal, SendMessageRequest.model_validate(request.params), schedule
                )
            elif request.method == "GetTask":
                result = self.get_task(principal, GetTaskRequest.model_validate(request.params))
            elif request.method in {
                "CreateTaskPushNotificationConfig",
                "GetTaskPushNotificationConfig",
                "ListTaskPushNotificationConfigs",
                "DeleteTaskPushNotificationConfig",
            }:
                raise RPCError(
                    -32003, "Push notifications are not supported", "PUSH_NOTIFICATION_NOT_SUPPORTED"
                )
            elif request.method in {
                "SendStreamingMessage",
                "SubscribeToTask",
                "GetExtendedAgentCard",
                "ListTasks",
            }:
                raise RPCError(-32004, "Operation is not supported by this agent", "UNSUPPORTED_OPERATION")
            elif request.method == "CancelTask":
                raise RPCError(
                    -32002, "Use the approval API to reject an unapproved migration", "TASK_NOT_CANCELABLE"
                )
            else:
                raise RPCError(-32601, "Method not found")
            return {"jsonrpc": "2.0", "id": request.id, "result": result}
        except RPCError as exc:
            return rpc_error(request.id, exc.code, exc.message, exc.reason)
        except ValidationError:
            return rpc_error(
                request.id, -32602, "Invalid parameters; review the documented command and schema"
            )
        except PermissionError:
            # Authentication/authorization are transport-level HTTP errors.
            raise
        except Exception:
            return rpc_error(request.id, -32603, "Internal error; inspect the local run state")
