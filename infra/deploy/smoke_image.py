"""Exercise one local Docker image: API auth, local state and maintenance barrier.

Uses disposable fixtures only. No cloud calls, database URLs or production tokens.
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import secrets
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path


_STARTUP_HTTP_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})


def read_health(url: str, *, timeout: float = 3) -> dict:
    """Read an exact HTTP 200 JSON object; invalid responses fail the smoke test."""
    with urllib.request.urlopen(url, timeout=timeout) as response:
        if response.status != 200:
            raise RuntimeError(f"Health endpoint returned unexpected HTTP {response.status}")
        try:
            payload = json.load(response)
        except (ValueError, UnicodeError):
            raise RuntimeError("Health endpoint did not return valid JSON") from None
    if not isinstance(payload, dict):
        raise RuntimeError("Health endpoint did not return a JSON object")
    return payload


def wait_for_health(url: str, *, timeout_seconds: float = 60, interval_seconds: float = 1) -> dict:
    """Tolerate bounded startup races, without hiding bad health responses.

    Docker can publish a port before the application accepts requests. In that
    interval urllib may raise a raw ConnectionResetError or IncompleteRead, not
    only URLError. Diagnostics deliberately contain no response/error text.
    """
    if timeout_seconds <= 0 or interval_seconds <= 0:
        raise ValueError("Health wait and interval must be positive")
    deadline = time.monotonic() + timeout_seconds
    last_failure = "no successful probe"
    while (remaining := deadline - time.monotonic()) > 0:
        try:
            payload = read_health(url, timeout=min(3, remaining))
        except urllib.error.HTTPError as error:
            if error.code not in _STARTUP_HTTP_STATUSES:
                raise RuntimeError(f"Health endpoint returned unexpected HTTP {error.code}") from None
            last_failure = f"HTTP {error.code}"
        except (urllib.error.URLError, ConnectionError, TimeoutError,
                http.client.RemoteDisconnected, http.client.IncompleteRead) as error:
            last_failure = type(error).__name__
        else:
            if payload.get("status") == "ok":
                if time.monotonic() <= deadline:
                    return payload
                last_failure = "ready response arrived after deadline"
            else:
                last_failure = "HTTP 200 without status=ok"
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(interval_seconds, remaining))
    raise RuntimeError(
        f"Health endpoint did not become ready within {timeout_seconds:g}s ({last_failure})"
    )


def command(*args: str, check: bool = True) -> str:
    result = subprocess.run(args, check=False, capture_output=True, text=True, timeout=120)
    if check and result.returncode:
        raise RuntimeError(f"Container smoke-test command failed: {args[0]} {args[1]}")
    return result.stdout.strip()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("image")
    args = parser.parse_args()
    if args.image.startswith("-"):
        parser.error("Image cannot be an option")
    name = "migration-smoke-" + secrets.token_hex(6)
    with tempfile.TemporaryDirectory(prefix="migration-smoke-") as directory:
        root = Path(directory)
        state = root / "state"
        state.mkdir()
        token = secrets.token_urlsafe(36)
        environment = root / "runtime.env"
        environment.write_text("MIGRATION_API_TOKEN=" + token + "\nMIGRATION_STATE_DIR=/state\n")
        environment.chmod(0o600)
        mount = f"type=bind,src={state},dst=/state"
        try:
            command("docker", "run", "--rm", "--user", "0:0", "--mount", mount,
                    "--entrypoint", "python", args.image, "-c",
                    "import os; os.chown('/state',10001,10001); os.chmod('/state',0o700)")
            command("docker", "run", "--detach", "--name", name, "--user", "10001:10001",
                    "--read-only", "--tmpfs", "/tmp:rw,nosuid,nodev,size=256m", "--cap-drop", "ALL",
                    "--security-opt", "no-new-privileges", "--publish", "127.0.0.1::8000",
                    "--env-file", str(environment), "--mount", mount, args.image)
            port = command("docker", "port", name, "8000/tcp").splitlines()[0].rsplit(":", 1)[1]
            if not port.isdecimal():
                raise RuntimeError("Docker returned an unexpected port")
            origin = "http://127.0.0.1:" + port

            def health() -> dict:
                return read_health(origin + "/health")

            initial = wait_for_health(origin + "/health")
            if initial.get("active_runs") != 0 or initial.get("draining") is not False:
                raise RuntimeError("Initial maintenance state is invalid")
            try:
                urllib.request.urlopen(origin + "/runs/missing-smoke-run", timeout=3)
            except urllib.error.HTTPError as error:
                if error.code != 401:
                    raise RuntimeError("Unauthenticated API call was not rejected") from error
            else:
                raise RuntimeError("Unauthenticated API call unexpectedly succeeded")
            for enabled in (True, False):
                command("docker", "exec", name, "datamigrate", "maintenance", "on" if enabled else "off",
                        "--workspace", "/state")
                if health().get("draining") is not enabled:
                    raise RuntimeError("API and CLI do not share the maintenance gate")
            print("Container health, API authentication and persistent maintenance barrier passed")
        finally:
            command("docker", "rm", "--force", name, check=False)
            # Return ownership of disposable files so the unprivileged CI runner
            # can remove them without a host sudo command.
            command("docker", "run", "--rm", "--user", "0:0", "--mount", mount,
                    "--entrypoint", "python", args.image, "-c",
                    "import os; "
                    f"uid,gid={os.getuid()},{os.getgid()}; "
                    "[(os.chown(p,uid,gid)) for d,ds,fs in os.walk('/state') "
                    "for p in [d]+[os.path.join(d,n) for n in ds+fs]]", check=False)


if __name__ == "__main__":
    main()
