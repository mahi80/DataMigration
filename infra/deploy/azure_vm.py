"""Deploy to an existing Azure VM through Run Command using the caller's role."""

from __future__ import annotations

import argparse
import json
import re
import subprocess

from remote import remote_script, success_marker


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--resource-group", required=True)
    parser.add_argument("--vm-name", required=True)
    parser.add_argument("--acr-name", required=True)
    parser.add_argument("--image", required=True)
    args = parser.parse_args()
    for value in (args.resource_group, args.vm_name):
        if not re.fullmatch(r"[A-Za-z0-9_.()-]{1,90}", value):
            parser.error("Invalid VM/resource-group identifier")
    script = remote_script(args.image, "azure", acr_name=args.acr_name)
    try:
        result = subprocess.run([
            "az", "vm", "run-command", "invoke", "--resource-group", args.resource_group,
            "--name", args.vm_name, "--command-id", "RunShellScript", "--scripts", script,
            "--output", "json", "--only-show-errors",
        ], capture_output=True, text=True, timeout=1500, check=False)
    except subprocess.TimeoutExpired:
        raise TimeoutError("Azure deployment outcome is unresolved; inspect the VM before another attempt") from None
    if result.returncode != 0:
        raise RuntimeError("Azure Run Command failed; inspect the restricted deployment/VM logs")
    body = json.loads(result.stdout)
    # Azure provisioning success alone does not prove the shell script or health
    # checks succeeded. Require the final host-generated success marker.
    outputs = body.get("value", [])
    if not any(success_marker(args.image) in [line.strip() for line in value.get("message", "").splitlines()] for value in outputs):
        raise RuntimeError("Azure command returned without verified healthy deployment; inspect VM logs")
    print("Verified Azure VM deployment and application health")


if __name__ == "__main__":
    main()
