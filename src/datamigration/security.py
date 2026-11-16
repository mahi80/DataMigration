"""Server-owned identities and roles shared by HTTP, MCP, and A2A boundaries.

This policy controls migration operations, not database row permissions. Tokens
are configured by the operator, hashed in memory, and never loaded from a tool
argument or a request-body role. Database permissions remain independently
enforced by database credentials and cloud identity.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping


class Role(str, Enum):
    VIEWER = "viewer"
    PLANNER = "planner"
    APPROVER = "approver"
    OPERATOR = "operator"
    ADMIN = "admin"


@dataclass(frozen=True)
class Principal:
    subject: str
    role: Role
    allowed_connections: frozenset[str] | None = None
    allowed_tables: Mapping[str, frozenset[str]] | None = None
    connection_options: Mapping[str, dict[str, Any]] | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.subject, str)
            or not self.subject.strip()
            or len(self.subject) > 200
            or any(ord(character) < 32 for character in self.subject)
        ):
            raise ValueError("A configured principal needs a nonempty identity of at most 200 characters")
        try:
            object.__setattr__(self, "role", Role(self.role))
        except ValueError:
            raise ValueError("Unknown configured migration role") from None
        if self.allowed_connections is not None:
            if isinstance(self.allowed_connections, str) or not all(
                isinstance(name, str) and name for name in self.allowed_connections
            ):
                raise ValueError("allowed_connections must contain environment-variable references")
            object.__setattr__(self, "allowed_connections", frozenset(self.allowed_connections))
        if self.allowed_tables is not None:
            if not isinstance(self.allowed_tables, Mapping):
                raise ValueError("allowed_tables must map connection environment references to table names")
            normalized = {}
            for variable, names in self.allowed_tables.items():
                if (
                    not isinstance(variable, str)
                    or isinstance(names, str)
                    or not all(isinstance(name, str) and name for name in names)
                ):
                    raise ValueError("Invalid configured table scope")
                normalized[variable] = frozenset(names)
            object.__setattr__(self, "allowed_tables", MappingProxyType(normalized))
        if self.connection_options is not None:
            if not isinstance(self.connection_options, Mapping) or any(
                not isinstance(value, dict) for value in self.connection_options.values()
            ):
                raise ValueError(
                    "connection_options must map connection environment references to approved options"
                )
            object.__setattr__(
                self,
                "connection_options",
                MappingProxyType(json.loads(json.dumps(dict(self.connection_options)))),
            )


class AuthenticationError(PermissionError):
    """No configured credential matched the request."""


class AuthorizationError(PermissionError):
    """The authenticated identity lacks the requested action."""


_COMMON = frozenset({"connectors", "status"})
_PLANNING = frozenset({"discover", "plan", "read_plan"})
ROLE_ACTIONS = {
    Role.VIEWER: _COMMON,
    Role.PLANNER: _COMMON | _PLANNING,
    Role.APPROVER: _COMMON | {"read_plan", "approve"},
    Role.OPERATOR: _COMMON | _PLANNING | {"retry"},
    Role.ADMIN: _COMMON | _PLANNING | {"approve", "retry"},
}


class RolePolicy:
    @staticmethod
    def allows(principal: Principal, action: str) -> bool:
        return action in ROLE_ACTIONS[principal.role]

    @staticmethod
    def require(principal: Principal, action: str) -> None:
        if not RolePolicy.allows(principal, action):
            raise AuthorizationError("The authenticated role is not authorized for this operation")

    @staticmethod
    def connection(
        principal: Principal, connection: Any, tables: list[str] | None = None
    ) -> frozenset[str] | None:
        variable = connection.url_env
        if principal.allowed_connections is not None and variable not in principal.allowed_connections:
            raise AuthorizationError("The connection is outside the authenticated identity's scope")
        allowed = None
        if principal.allowed_tables is not None:
            if variable not in principal.allowed_tables:
                raise AuthorizationError("No table scope is configured for this connection")
            allowed = principal.allowed_tables[variable]
            if tables is not None and not set(tables) <= allowed:
                raise AuthorizationError("A table is outside the authenticated identity's scope")
        if (
            principal.allowed_connections is not None
            or principal.allowed_tables is not None
            or principal.connection_options is not None
        ):
            expected = (principal.connection_options or {}).get(variable, {})
            if connection.options != expected:
                raise AuthorizationError(
                    "Scoped connections must use their server-configured connection options"
                )
        return allowed

    @staticmethod
    def spec(principal: Principal, spec: Any) -> None:
        RolePolicy.connection(principal, spec.source, [table.source for table in spec.tables])
        RolePolicy.connection(principal, spec.target, [table.target for table in spec.tables])


def _principal(value: Any, position: int) -> Principal:
    if isinstance(value, Principal):
        return value
    if isinstance(value, str):
        return Principal(f"configured-{value}-{position}", Role(value))
    fields = {"subject", "role", "allowed_connections", "allowed_tables", "connection_options"}
    if isinstance(value, Mapping) and {"subject", "role"} <= set(value) <= fields:
        return Principal(**value)
    raise ValueError("Each token binding must specify a role or an explicit subject and role")


class TokenAuthenticator:
    """Small single-workspace token authenticator; not an identity provider.

    ``token_roles`` maps runtime token values to a role, Principal, or
    ``{subject, role}``. For environment configuration, use
    MIGRATION_API_TOKEN_ROLES as a JSON array of {token_env, subject, role}; the
    JSON contains environment-variable names, never the token values.
    """

    def __init__(self, token_roles: Mapping[str, Any]):
        self._bindings: list[tuple[bytes, Principal]] = []
        for position, (token, identity) in enumerate(token_roles.items()):
            if (
                not isinstance(token, str)
                or len(token) < 24
                or any(character.isspace() or ord(character) < 32 for character in token)
            ):
                raise ValueError("Configured API tokens must contain at least 24 non-whitespace characters")
            self._bindings.append((hashlib.sha256(token.encode()).digest(), _principal(identity, position)))
        if not self._bindings:
            raise ValueError("Configure MIGRATION_API_TOKEN or explicit server-side token role bindings")

    @classmethod
    def from_config(
        cls,
        token: str | None = None,
        token_roles: Mapping[str, Any] | None = None,
    ) -> "TokenAuthenticator":
        bindings = dict(token_roles) if token_roles is not None else {}
        if token is not None:
            if token in bindings:
                raise ValueError("A legacy administrator token cannot also have a separate role binding")
            bindings[token] = Principal("legacy-api-admin", Role.ADMIN)
        elif token_roles is None:
            configured = os.environ.get("MIGRATION_API_TOKEN_ROLES")
            if configured:
                try:
                    records = json.loads(configured)
                except (ValueError, TypeError):
                    raise ValueError("MIGRATION_API_TOKEN_ROLES must be a valid JSON array") from None
                if not isinstance(records, list) or not records:
                    raise ValueError("MIGRATION_API_TOKEN_ROLES must be a nonempty JSON array")
                for record in records:
                    fields = {
                        "token_env",
                        "subject",
                        "role",
                        "allowed_connections",
                        "allowed_tables",
                        "connection_options",
                    }
                    if (
                        not isinstance(record, dict)
                        or not {"token_env", "subject", "role"} <= set(record) <= fields
                    ):
                        raise ValueError(
                            "Token role records require token_env, subject, role, and optional server resource scopes"
                        )
                    variable = record["token_env"]
                    if not isinstance(variable, str) or not variable:
                        raise ValueError("token_env must name a configured environment variable")
                    secret = os.environ.get(variable)
                    if not secret:
                        raise ValueError("A configured role token environment variable is missing")
                    if secret in bindings:
                        raise ValueError("Each configured API token must have exactly one identity")
                    bindings[secret] = _principal(
                        {key: value for key, value in record.items() if key != "token_env"}, len(bindings)
                    )
            else:
                legacy = os.environ.get("MIGRATION_API_TOKEN")
                if legacy:
                    bindings[legacy] = Principal("legacy-api-admin", Role.ADMIN)
        return cls(bindings)

    def authenticate(self, authorization: str) -> Principal:
        scheme, separator, token = authorization.partition(" ")
        if not separator or scheme.lower() != "bearer" or not token or " " in token:
            raise AuthenticationError("Authentication required")
        supplied = hashlib.sha256(token.encode()).digest()
        matched = None
        for expected, principal in self._bindings:
            if secrets.compare_digest(supplied, expected):
                matched = principal
        if matched is None:
            raise AuthenticationError("Authentication required")
        return matched


def local_principal() -> Principal:
    """Read a local stdio principal once at process startup.

    The process owner's environment is trusted. Changing a tool argument cannot
    change this identity. The default operator can plan/retry but cannot approve.
    """
    connections = os.environ.get("MIGRATION_MCP_ALLOWED_CONNECTIONS")
    tables = os.environ.get("MIGRATION_MCP_ALLOWED_TABLES")
    options = os.environ.get("MIGRATION_MCP_CONNECTION_OPTIONS")
    return Principal(
        os.environ.get("MIGRATION_MCP_SUBJECT", "local-operator"),
        Role(os.environ.get("MIGRATION_MCP_ROLE", "operator")),
        frozenset(name.strip() for name in connections.split(",") if name.strip())
        if connections is not None
        else None,
        json.loads(tables) if tables is not None else None,
        json.loads(options) if options is not None else None,
    )
