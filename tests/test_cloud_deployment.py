"""Deployment contracts with fake cloud responses; never run host mutations."""

from __future__ import annotations

import base64
import importlib
import json
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

DEPLOY = Path(__file__).resolve().parents[1] / "infra" / "deploy"
AWS_IMAGE = "123456789012.dkr.ecr.eu-west-1.amazonaws.com/migration@sha256:" + "a" * 64
AZURE_IMAGE = "migrationregistry.azurecr.io/migration@sha256:" + "b" * 64


@pytest.fixture
def deployment_modules(monkeypatch):
    monkeypatch.syspath_prepend(str(DEPLOY))
    return tuple(importlib.import_module(name) for name in ("remote", "aws_ssm", "azure_vm"))


@pytest.mark.parametrize("image,provider,options", [
    (AWS_IMAGE + "; touch /tmp/unwanted", "aws", {"region": "eu-west-1"}),
    (AWS_IMAGE.replace("@sha256:", ":latest"), "aws", {"region": "eu-west-1"}),
    (AWS_IMAGE, "aws", {"region": "us-east-1"}),
    (AWS_IMAGE, "aws", {"region": "eu-west-1\nwhoami"}),
    (AZURE_IMAGE, "azure", {"acr_name": "otherregistry"}),
    (AZURE_IMAGE, "azure", {"acr_name": "migrationregistry;whoami"}),
    (AZURE_IMAGE, "unknown", {}),
])
def test_remote_command_rejects_unsafe_or_mismatched_identity(deployment_modules, image, provider, options):
    remote, _, _ = deployment_modules
    with pytest.raises(ValueError):
        remote.remote_script(image, provider, **options)


def test_rendered_payload_preserves_reviewed_script_and_has_valid_shell_syntax(deployment_modules):
    remote, _, _ = deployment_modules
    script = remote.remote_script(AWS_IMAGE, "aws", region="eu-west-1")
    encoded = re.search(r"printf '%s' '([A-Za-z0-9+/=]+)'", script).group(1)
    assert base64.b64decode(encoded) == (DEPLOY / "deploy_host.sh").read_bytes()
    subprocess.run(["bash", "-n"], input=script, text=True, check=True)
    subprocess.run(["bash", "-n", str(DEPLOY / "deploy_host.sh")], check=True)


def test_aws_waits_for_native_receipt_and_exact_application_marker(deployment_modules, monkeypatch):
    remote, aws, _ = deployment_modules
    class NotVisible(Exception):
        pass
    responses = [NotVisible(), {"Status": "InProgress"}, {
        "Status": "Success", "ResponseCode": 0, "StandardOutputContent": remote.success_marker(AWS_IMAGE) + "\n",
    }]
    requests = []
    def poll(**kwargs):
        result = responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result
    def send(**kwargs):
        requests.append(kwargs)
        return {"Command": {"CommandId": "command-id"}}
    client = SimpleNamespace(send_command=send, get_command_invocation=poll,
                             exceptions=SimpleNamespace(InvocationDoesNotExist=NotVisible))
    monkeypatch.setitem(sys.modules, "boto3", SimpleNamespace(client=lambda *args, **kwargs: client))
    monkeypatch.setattr(aws.time, "sleep", lambda _: None)
    monkeypatch.setattr(sys, "argv", ["aws_ssm.py", "--instance-id", "i-0123456789abcdef0",
                                    "--region", "eu-west-1", "--image", AWS_IMAGE])
    aws.main()
    assert len(requests) == 1
    assert requests[0]["InstanceIds"] == ["i-0123456789abcdef0"]
    assert requests[0]["DocumentName"] == "AWS-RunShellScript"
    assert responses == []


@pytest.mark.parametrize("message", ["", "application failed", "DATAMIGRATION_DEPLOY_OK=sha256:" + "b" * 64 + "extra"])
def test_azure_provisioning_success_does_not_prove_application_success(deployment_modules, monkeypatch, message):
    _, _, azure = deployment_modules
    result = SimpleNamespace(returncode=0, stdout=json.dumps({"value": [{"code": "ProvisioningState/succeeded", "message": message}]}))
    monkeypatch.setattr(azure.subprocess, "run", lambda *args, **kwargs: result)
    monkeypatch.setattr(sys, "argv", ["azure_vm.py", "--resource-group", "migration-rg", "--vm-name", "migration-vm",
                                    "--acr-name", "migrationregistry", "--image", AZURE_IMAGE])
    with pytest.raises(RuntimeError, match="without verified"):
        azure.main()


def test_azure_accepts_only_exact_digest_marker(deployment_modules, monkeypatch):
    remote, _, azure = deployment_modules
    result = SimpleNamespace(returncode=0, stdout=json.dumps({"value": [{"message": "[stdout]\n" + remote.success_marker(AZURE_IMAGE) + "\n[stderr]"}]}))
    calls = []
    def run(*args, **kwargs):
        calls.append((args, kwargs))
        return result
    monkeypatch.setattr(azure.subprocess, "run", run)
    monkeypatch.setattr(sys, "argv", ["azure_vm.py", "--resource-group", "migration-rg", "--vm-name", "migration-vm",
                                    "--acr-name", "migrationregistry", "--image", AZURE_IMAGE])
    azure.main()
    assert calls[0][0][0][:4] == ["az", "vm", "run-command", "invoke"]
    assert calls[0][1]["capture_output"] is True


def test_azure_timeout_stays_unknown_without_retry(deployment_modules, monkeypatch):
    _, _, azure = deployment_modules
    calls = []
    def timeout(*args, **kwargs):
        calls.append(args)
        raise subprocess.TimeoutExpired("az", 1500)
    monkeypatch.setattr(azure.subprocess, "run", timeout)
    monkeypatch.setattr(sys, "argv", ["azure_vm.py", "--resource-group", "migration-rg", "--vm-name", "migration-vm",
                                    "--acr-name", "migrationregistry", "--image", AZURE_IMAGE])
    with pytest.raises(TimeoutError, match="unresolved"):
        azure.main()
    assert len(calls) == 1
