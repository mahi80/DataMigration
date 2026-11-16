"""Deploy to an existing SSM-managed EC2 host using the caller's scoped IAM role."""

from __future__ import annotations

import argparse
import re
import time

from remote import remote_script, success_marker


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--instance-id", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--image", required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"i-[a-f0-9]{8,17}", args.instance_id):
        parser.error("Invalid EC2 instance ID")
    script = remote_script(args.image, "aws", region=args.region)
    import boto3
    client = boto3.client("ssm", region_name=args.region)
    response = client.send_command(
        InstanceIds=[args.instance_id], DocumentName="AWS-RunShellScript",
        Parameters={"commands": [script], "executionTimeout": ["1200"]},
        TimeoutSeconds=120, Comment="Deploy reviewed DataMigration image by immutable digest",
    )
    command_id = response["Command"]["CommandId"]
    print(f"Submitted SSM command {command_id}; waiting for verified application health", flush=True)
    deadline = time.monotonic() + 1500
    while time.monotonic() < deadline:
        try:
            result = client.get_command_invocation(CommandId=command_id, InstanceId=args.instance_id)
        except client.exceptions.InvocationDoesNotExist:
            time.sleep(3)
            continue
        status = result["Status"]
        if status in {"Pending", "InProgress", "Delayed"}:
            time.sleep(5)
            continue
        lines = [line.strip() for line in result.get("StandardOutputContent", "").splitlines()]
        if status == "Success" and result.get("ResponseCode") == 0 and success_marker(args.image) in lines:
            print(f"Verified AWS deployment through SSM command {command_id}")
            return
        raise RuntimeError(f"AWS deployment did not verify successfully; inspect restricted SSM command {command_id}")
    raise TimeoutError(f"SSM command {command_id} is unresolved; inspect it before another deployment")


if __name__ == "__main__":
    main()
