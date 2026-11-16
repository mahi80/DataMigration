"""File-backed CLI hosting for the native cloud LangGraph workflow."""

from __future__ import annotations

import json
import os
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from filelock import FileLock
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from .cloud.base import config_for
from .cloud.workflow import build_cloud_graph
from .maintenance import submission_guard
from .spec import reject_inline_secrets, write_json
from .workflow import safe_error


class CloudRunManager:
    def __init__(self, workspace: str | Path = ".migration"):
        self.workspace = Path(workspace).resolve()
        self.workspace.mkdir(parents=True, exist_ok=True, mode=0o700)

    def directory(self, run_id: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", run_id):
            raise ValueError("Invalid cloud run ID")
        return self.workspace / "cloud-runs" / run_id

    def metadata(self, run_id: str) -> dict:
        return json.loads((self.directory(run_id) / "run.json").read_text())

    @contextmanager
    def graph(self, run_id: str):
        metadata = self.metadata(run_id)
        connection = sqlite3.connect(self.workspace / "cloud-checkpoints.sqlite", check_same_thread=False, timeout=30)
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            yield build_cloud_graph(SqliteSaver(connection), metadata["provider"], metadata["executor_config"])
        finally:
            connection.close()

    def _invoke(self, run_id: str, payload):
        metadata = self.metadata(run_id)
        metadata.pop("error", None)
        metadata["status"] = "RUNNING"
        path = self.directory(run_id) / "run.json"
        with submission_guard(self.workspace):
            write_json(path, metadata)
        try:
            with self.graph(run_id) as graph:
                result = graph.invoke(payload, {"configurable": {"thread_id": run_id}}, durability="sync")
            metadata["status"] = result["status"]
            if "bundle" in result:
                write_json(self.directory(run_id) / "bundle.json", result["bundle"])
        except Exception as exc:
            metadata["status"] = "ERROR"
            metadata["error"] = safe_error(exc)
            write_json(path, metadata)
            raise
        write_json(path, metadata)
        return self.status(run_id)

    def start(self, provider: str, spec: dict, run_id: str | None = None) -> dict:
        reject_inline_secrets(spec)
        run_id = run_id or str(uuid4())
        directory = self.directory(run_id)
        directory.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        config = config_for(spec, provider)
        config["ledger_path"] = str(self.workspace / "cloud-submissions.sqlite")
        if provider == "databricks":
            config["host"] = config.get("host") or os.environ.get("DATABRICKS_HOST")
        with FileLock(str(directory) + ".lock", timeout=0):
            directory.mkdir(mode=0o700)
            write_json(directory / "run.json", {"run_id": run_id, "provider": provider, "executor_config": config,
                                                "status": "CREATED", "spec": spec})
            return self._invoke(run_id, {"spec": spec, "operation_id": run_id})

    def status(self, run_id: str) -> dict:
        metadata = self.metadata(run_id)
        with self.graph(run_id) as graph:
            snapshot = graph.get_state({"configurable": {"thread_id": run_id}})
        state = snapshot.values or {}
        result = {"run_id": run_id, "provider": metadata["provider"], "status": metadata["status"],
                  "bundle_hash": state.get("bundle_hash"), "execution_id": state.get("execution_id"),
                  "artifact_directory": str(self.directory(run_id)), "next_nodes": list(snapshot.next)}
        if metadata.get("error"):
            result["error"] = metadata["error"]
        if state.get("native_result"):
            result["native_result"] = state["native_result"]
        return result

    def approve(self, run_id: str, bundle_hash: str, reviewer: str, *, approved: bool = True) -> dict:
        if not reviewer.strip() or len(reviewer) > 200:
            raise ValueError("Reviewer identity of 1-200 characters is required")
        with FileLock(str(self.directory(run_id)) + ".lock", timeout=0):
            current = self.status(run_id)
            if current["status"] != "AWAITING_APPROVAL" or current["bundle_hash"] != bundle_hash:
                raise ValueError("Cloud approval must match the pending bundle and hash")
            return self._invoke(run_id, Command(resume={"approve": approved, "bundle_hash": bundle_hash, "reviewer": reviewer}))

    def poll(self, run_id: str) -> dict:
        with FileLock(str(self.directory(run_id)) + ".lock", timeout=0):
            if self.status(run_id)["status"] != "WAITING_FOR_NATIVE_STATUS":
                raise ValueError("Cloud run is not awaiting native status")
            return self._invoke(run_id, Command(resume={"action": "poll"}))

    def retry(self, run_id: str) -> dict:
        with FileLock(str(self.directory(run_id)) + ".lock", timeout=0):
            current = self.status(run_id)
            if current["status"] not in {"ERROR", "RUNNING", "CREATED"}:
                raise ValueError("Only interrupted/API-error runs can resume; native failed jobs need reviewed recovery")
            with self.graph(run_id) as graph:
                state = graph.get_state({"configurable": {"thread_id": run_id}})
            initial = None if state.values else {"spec": self.metadata(run_id)["spec"], "operation_id": run_id}
            return self._invoke(run_id, initial)
