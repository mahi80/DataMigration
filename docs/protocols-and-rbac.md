# Roles, resource scopes, MCP, and A2A

The API, MCP server, and A2A interface share the same deterministic migration
workflow. They discover schemas, create a durable plan, and stop for approval.
Only an authenticated approver or administrator can approve that exact plan
through the HTTP API. The core worker still checks the plan hash, connection
binding, source schema, staged data, and batch receipts before completing a run.

## 1. Identity and permissions

A role comes from server configuration. A request cannot set a role, grant a
connection scope, impersonate a reviewer, or bypass the LangGraph interrupt.
`X-Role` and `X-Subject` headers have no authority. Request models reject
unexpected role and scope fields.

| Operation | Viewer | Planner | Approver | Operator | Admin |
|---|---|---|---|---|---|
| Connector capabilities and run status | Yes | Yes | Yes | Yes | Yes |
| Source/target table discovery | — | Yes | — | Yes | Yes |
| Create a plan and stop for approval | — | Yes | — | Yes | Yes |
| Read the exact plan | — | Yes | Yes | Yes | Yes |
| Approve or reject a plan | — | — | Yes | — | Yes |
| Retry an interrupted or failed run | — | — | — | Yes | Yes |

An operator can retry an already approved run; a retry does not grant new
approval. The planner and operator roles cannot authorize destination writes.
The `reviewer` field remains accepted by the HTTP approval request for backwards
compatibility, but the approval ledger always records the **authenticated
principal's configured subject**.

This is a single-workspace authorization policy. It is not a multi-tenant
identity platform, an OAuth issuer, database row-level security, or a replacement
for IAM, database grants, Unity Catalog permissions, and storage permissions.
Database credentials must independently restrict what each deployment can read
or write. For external deployment, terminate TLS at a trusted ingress and
configure identity/token lifecycle management there.

### Configure role tokens

Set tokens through the environment or a secret manager. `MIGRATION_API_TOKEN_ROLES`
contains references to token environment variables rather than their values:

```bash
export MIGRATION_API_TOKEN_ROLES='[
  {"token_env":"PLANNER_TOKEN","subject":"migration-planner","role":"planner"},
  {"token_env":"APPROVER_TOKEN","subject":"data-owner","role":"approver"},
  {"token_env":"VIEWER_TOKEN","subject":"operations-dashboard","role":"viewer"}
]'
export MIGRATION_PUBLIC_URL='https://migration.example.com'

# Supply PLANNER_TOKEN, APPROVER_TOKEN and VIEWER_TOKEN from your secret manager.
# Each configured token must have at least 24 non-whitespace characters.
datamigrate serve --host 127.0.0.1 --port 8000 --workspace .migration
```

For trusted local development, the legacy `MIGRATION_API_TOKEN` remains a single
administrator credential. It is used only when the role-token configuration is
absent. Configuring `MIGRATION_API_TOKEN_ROLES` does not leave an implicit legacy
administrator token enabled. The Python factory also accepts
`create_app(workspace, token=...)` for the legacy administrator or
`create_app(workspace, token_roles={token_value: {"subject": ..., "role": ...}})`
for explicit runtime configuration. Tokens are hashed in memory and are not
written into run artifacts.

The local CLI still runs as the trusted operating-system user and is not a
remote authorization boundary. Restrict access to its workspace and credentials.

### Restrict connections and tables

Optional scopes are configured on each principal:

```json
{
  "token_env": "PLANNER_TOKEN",
  "subject": "commerce-planner",
  "role": "planner",
  "allowed_connections": ["COMMERCE_SOURCE", "ANALYTICS_TARGET"],
  "allowed_tables": {
    "COMMERCE_SOURCE": ["public.customers", "public.orders"],
    "ANALYTICS_TARGET": ["warehouse.customers", "warehouse.orders"]
  },
  "connection_options": {
    "COMMERCE_SOURCE": {},
    "ANALYTICS_TARGET": {}
  }
}
```

Connection names refer to the `url_env` field, never to a supplied URI. Table
names match the exact logical names used by that connector. Prefer fully
qualified SQL names so the permitted namespace is visible. No wildcard matching
is performed.

When `allowed_tables` is present, each referenced connection must have an entry;
an empty table list allows no tables. Discovery filters the inventory to this
scope and rejects explicit out-of-scope requests. Planning checks both source
and target mappings. Status, plan reads, approval, and retry check the run's
resources against the caller's current scope.

For any scoped principal, connection options must exactly match the corresponding
server-configured `connection_options` entry, whose default is `{}`. This prevents
a client from keeping an allowed connection name while changing its database,
schema, host override, file mapping, or query configuration. For Databricks,
configure the required warehouse path, token environment reference, and namespace
options on the server and use the same options in requests. A configured query
projection delegates access to that projection; native database privileges must
also restrict its underlying tables.

Omitting scopes gives that explicitly configured identity access to all resources
in this one workspace that its action role permits. Use scopes for remote service
identities. Scopes are never accepted from the request body.

## 2. HTTP operations

All routes below require `Authorization: Bearer <configured-token>`, except
`/health` and the public A2A Agent Card.

| Route | Purpose | Required action |
|---|---|---|
| `GET /connectors` | Return connector capabilities and verification levels | Status access |
| `POST /discover` | List/inspect a selected source or target database/namespace | Discover |
| `POST /runs` | Prepare a migration and pause for review | Plan |
| `GET /runs/{id}` | Read lifecycle and validation outcomes | Status access |
| `GET /runs/{id}/plan` | Read the integrity-checked plan | Read plan |
| `POST /runs/{id}/approval` | Approve/reject the exact hash | Approve |
| `POST /runs/{id}/retry` | Resume an eligible interrupted/failed run | Retry |
| `GET /health` | Read health, `draining`, and `active_runs` | Public |

For example, source discovery accepts:

```json
{
  "connection": {
    "kind": "postgresql",
    "url_env": "COMMERCE_SOURCE",
    "options": {}
  },
  "scope": "source",
  "tables": ["public.customers"],
  "include_schemas": true,
  "max_tables": 100
}
```

Use `scope: "target"` and a target connection to inspect the destination. Omitting
`tables` lists the permitted tables in the configured database or namespace.
The response includes column types, nullability, keys where the connector can
inspect them, and `raw_samples_included: false`. It never returns row samples or
a resolved connection URI. It does not enumerate every database on a server;
select a database/namespace through a configured connection.

HTTP authentication failures return 401, insufficient roles/scopes return 403,
and invalid requests return 422 without echoing input values. Connector and run
errors are redacted. Health and status remain available while the deployment is
draining; submission and execution admission use the core maintenance guard.

## 3. MCP stdio server

The MCP implementation uses the **official Python SDK**, specifically its
maintained v1 API, `mcp.server.fastmcp.FastMCP`. The optional dependency is pinned
to `mcp>=1.28,<2`; the current v2 SDK renamed that class and import path. This is
a deliberate SDK compatibility choice, not a hand-written substitute transport.
See the [official v1 documentation](https://py.sdk.modelcontextprotocol.io/v1/)
and [SDK migration guide](https://py.sdk.modelcontextprotocol.io/migration/).

```bash
pip install -e '.[mcp]'
export MIGRATION_MCP_ROLE=planner
export MIGRATION_MCP_SUBJECT=local-planning-client
python -m datamigration.protocols.mcp_server --workspace .migration
```

The process speaks MCP over stdin/stdout; diagnostic logging goes to stderr.
Configure your MCP host to start that command and provide the approved connection
environment variables. No HTTP port or MCP bearer-token server is started.

The principal is captured once at process startup. `MIGRATION_MCP_ROLE` defaults
to `operator` and `MIGRATION_MCP_SUBJECT` defaults to `local-operator`. This is a
trusted local process model: the process owner controls the environment. A remote
client cannot choose that identity through a tool argument.

Optional local resource scopes use:

- `MIGRATION_MCP_ALLOWED_CONNECTIONS`: comma-separated `url_env` names.
- `MIGRATION_MCP_ALLOWED_TABLES`: JSON object mapping each connection to exact table names.
- `MIGRATION_MCP_CONNECTION_OPTIONS`: JSON object of server-approved options per connection.

| MCP tool | Input | Result |
|---|---|---|
| `list_connectors` | No arguments | Capabilities and verification levels |
| `discover_tables` | `request`: the discovery request above | Schema inventory without row samples |
| `plan_migration` | `spec`: MigrationSpec; optional `run_id` | Durable run paused for approval |
| `get_run_status` | `run_id` | Lifecycle and validation outcomes |
| `get_migration_plan` | `run_id` | The exact reviewed-plan candidate |

Tool listings reflect the process role, and invocation checks authorization again.
Tool arguments cannot set the role or scope. There is deliberately no approval,
raw-SQL execution, arbitrary stored-procedure, shell, or unrestricted filesystem
tool. Even an administrator MCP process has no approval tool: an independent
caller must use the approval API. Database discovery can use connector drivers;
MCP itself is not a database protocol.

## 4. A2A 1.0 interface

The same HTTP application serves:

- `GET /.well-known/agent-card.json`: public, cacheable Agent Card.
- `POST /a2a`: authenticated A2A **1.0 JSON-RPC**.

Set `MIGRATION_PUBLIC_URL` to the externally reachable application base URL so
the Agent Card advertises the correct address. It defaults to loopback development
at `http://127.0.0.1:8000`; a non-loopback URL must use HTTPS. The card declares
`JSONRPC`, protocol version `1.0`, bearer authentication, structured JSON input,
and no streaming or push notifications.

This implementation follows the current
[A2A specification](https://a2a-protocol.org/v1.0.0/specification/) and the
[normative protocol definition](https://github.com/a2aproject/A2A/blob/v1.0.0/specification/a2a.proto).
A2A 1.0 uses `SendMessage` / `GetTask`, uppercase enum names, and
`securityRequirements` in the Agent Card. Older `message/send` and `tasks/get`
requests are not silently treated as 1.0. Send `A2A-Version: 1.0`; an omitted
header implies the old version under the specification and is rejected by this
1.0-only interface.

### Submit a planning task

Send a planner or operator token and this JSON-RPC envelope:

```json
{
  "jsonrpc": "2.0",
  "id": "request-1",
  "method": "SendMessage",
  "params": {
    "message": {
      "messageId": "client-generated-unique-message-id",
      "role": "ROLE_USER",
      "parts": [
        {
          "mediaType": "application/json",
          "data": {
            "operation": "plan",
            "spec": {
              "spec_version": 1,
              "name": "customer-copy",
              "source": {"kind": "postgresql", "url_env": "COMMERCE_SOURCE"},
              "target": {"kind": "postgresql", "url_env": "ANALYTICS_TARGET"},
              "tables": [
                {"source": "public.customers", "target": "warehouse.customers", "keys": ["customer_id"]}
              ],
              "policy": {"source_quiesced": true}
            }
          }
        }
      ]
    },
    "configuration": {"returnImmediately": false, "historyLength": 0}
  }
}
```

`ROLE_USER` is the A2A message-sender enum. It has no relationship to the caller's
RBAC role; the bearer token supplies that identity. The `{operation, spec}` data
object is this agent skill's application payload inside a standard A2A `Part`.
There is no natural-language SQL execution or implicit interpretation of an
approval instruction.

A successful planning response contains `result.task` with a server-generated
ID and state `TASK_STATE_INPUT_REQUIRED`. Its status artifact provides the run
ID, plan hash, authenticated plan URL, and approval URL. It does not claim the
data has migrated. An approver reads `GET /runs/{id}/plan` and then supplies the
exact hash to `POST /runs/{id}/approval`.

Poll the task with any suitably scoped identity that has status access:

```json
{
  "jsonrpc": "2.0",
  "id": "poll-1",
  "method": "GetTask",
  "params": {"id": "server-returned-task-id", "historyLength": 0}
}
```

The response's `result` is a standard A2A Task. It reaches
`TASK_STATE_COMPLETED` only after the core worker reports completed validation;
a rejected decision produces `TASK_STATE_REJECTED`. Terminal task snapshots
remain terminal. If a failed underlying migration is later recovered as an
operator action, inspect its ordinary run status or create a new task instead
of expecting a failed A2A task to become nonterminal.

`returnImmediately: true` returns `TASK_STATE_SUBMITTED` and schedules planning
in the local HTTP process. Task IDs, request hashes, and task state are stored
in SQLite; repeated identical `messageId` values for the same configured subject
reuse the task, while changed content is rejected. Raw input-message history is
not retained or returned. A process failure before its background callback starts
can leave a submitted task without a run; resubmit with a new message ID in that
case. If the core run exists, an authorized operator uses the normal retry flow.
Use a durable external work queue for production availability across worker loss.

The implemented subset is planning plus polling. Streaming, subscriptions,
push notifications, task listing, and extended cards return the standard
unsupported-operation errors. A2A cannot approve, execute arbitrary commands,
or bypass native database permissions. A2A supports interaction among agents;
MCP exposes this tool's capabilities to a local host. Neither protocol grants
additional execution authority.

## 5. Verification

The tests exercise actual SQLite planning and approved migration, server-owned
roles and scopes, denial of caller-supplied role changes, safe invalid-input
errors, A2A version/method routing, nonblocking task submission, duplicate message
IDs, and durable task lookup. Agent Cards, requests, responses, and tasks are
parsed by the official A2A Python SDK's protobuf models in development tests.
The MCP test starts the real stdio server, performs the official SDK handshake,
lists tools, calls a permitted tool, and verifies a denied invocation.

```bash
pytest tests/test_rbac.py tests/test_protocols.py
```

These are local protocol and authorization integration tests. They do not certify
an external identity provider, a deployed ingress, every MCP host, every A2A
client, or production database permissions.
