"""Render an approved host script; only constrained identifiers enter its argv."""

from __future__ import annotations

import base64
import re
import shlex
from pathlib import Path


def validated_image(image: str) -> str:
    if not re.fullmatch(r"[a-z0-9][a-z0-9./_-]+@sha256:[a-f0-9]{64}", image):
        raise ValueError("Require a fully qualified digest-pinned image")
    return image


def remote_script(image: str, provider: str, *, region: str = "", acr_name: str = "") -> str:
    validated_image(image)
    arguments = ["--image", image, "--provider", provider]
    if provider == "aws":
        if not re.fullmatch(r"[a-z]{2}(-[a-z]+)+-[0-9]+", region):
            raise ValueError("Invalid AWS region")
        if not re.fullmatch(r"[0-9]{12}\.dkr\.ecr\.[a-z0-9-]+\.amazonaws\.com(?:\.cn)?", image.split("/")[0]):
            raise ValueError("Expected ECR image")
        if image.split("/")[0].split(".")[3] != region:
            raise ValueError("ECR region differs from the approved AWS region")
        arguments += ["--region", region]
    elif provider == "azure":
        if not re.fullmatch(r"[a-zA-Z0-9]{5,50}", acr_name) or image.split("/")[0] != acr_name.lower() + ".azurecr.io":
            raise ValueError("Expected image from the approved ACR")
        arguments += ["--acr-name", acr_name]
    else:
        raise ValueError("Unknown provider")
    payload = base64.b64encode(Path(__file__).with_name("deploy_host.sh").read_bytes()).decode()
    # Payload is base64 and argv uses shell quoting after strict validation.
    return "\n".join([
        "set -eu", "script=$(mktemp /run/datamigration-deploy.XXXXXX)",
        "trap 'rm -f -- \"$script\"' EXIT",
        f"printf '%s' '{payload}' | base64 --decode > \"$script\"",
        "bash \"$script\" " + shlex.join(arguments),
    ])


def success_marker(image: str) -> str:
    return "DATAMIGRATION_DEPLOY_OK=" + validated_image(image).split("@", 1)[1]
