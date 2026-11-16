"""Compile reviewed execution bundles and submit them through cloud SDKs.

SDK imports are lazy: compiling plans does not require cloud credentials or SDKs.
"""

from __future__ import annotations

from typing import Any

from .base import CloudConfigurationError, Executor, SubmissionUnknown


def compile_plan(provider: str, spec: dict[str, Any]) -> dict[str, Any]:
    if provider == "aws":
        from .aws import compile_plan as compile_aws
        return compile_aws(spec)
    if provider == "azure":
        from .azure import compile_plan as compile_azure
        return compile_azure(spec)
    if provider == "databricks":
        from .databricks import compile_plan as compile_databricks
        return compile_databricks(spec)
    raise CloudConfigurationError(f"Unknown cloud provider: {provider}")


def get_executor(provider: str, config: dict[str, Any], *, client: Any = None) -> Executor:
    if provider == "aws":
        from .aws import AwsExecutor
        return AwsExecutor(config, client=client)
    if provider == "azure":
        from .azure import AzureExecutor
        return AzureExecutor(config, client=client)
    if provider == "databricks":
        from .databricks import DatabricksExecutor
        return DatabricksExecutor(config, client=client)
    raise CloudConfigurationError(f"Unknown cloud provider: {provider}")


__all__ = ["compile_plan", "get_executor", "CloudConfigurationError", "SubmissionUnknown"]
