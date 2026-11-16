# Product gaps and production roadmap

The Next.js console closes the largest usability gap in the initial workbench:
operators can discover tables, prepare explicit mappings, review approvals and
inspect validation without composing every request manually. It does not turn
the current single-host snapshot engine into a universally certified migration
platform. The remaining work should be prioritized against a concrete source,
target, data volume and cutover requirement.

## Current foundation

The repository provides a local snapshot execution contract, role and resource
scopes, exact-hash approvals, deterministic quality rules, keyed replay,
mapped-row reconciliation, metadata/ontology integration helpers and native
cloud job adapters. The console uses those existing contracts. Network adapter
coverage, model advice and cloud deployment status are qualified in the README
and verification record rather than advertised as universally production-ready.

## Prioritized work

| Priority | Gap | Concrete next implementation | Evidence required to close it |
| --- | --- | --- | --- |
| P0: shared pilot | Enterprise sign-in and token lifecycle | OIDC integration with the chosen identity provider, audience/issuer checks, session revocation, administrator lifecycle and role mapping | Sign-in/logout/revocation, wrong tenant/audience, expired-token and privilege-escalation tests |
| P0: first live migration | Source/target qualification | Select one pair, exact engine versions, representative schemas and volume; test permissions, types, keys and real network behavior | Repeatable live integration suite, restart/replay evidence, measured throughput and reconciliation |
| P0: delivery | Configured SonarQube and protected deployment | Set the actual server/project/token, enforce its gate, configure reviewers and narrow OIDC roles, then qualify both application images | Observed green CI/scan, failed-gate blocking test, smoke-tested images and reviewed pilot rollout |
| P0: operation | Private state and recovery procedure | Encrypted local disk, consistent backup/restore, run/artifact retention, disk-space monitoring and recovery ownership | Restore rehearsal and a run resumed without losing approval or submission receipts |
| P1: longer jobs | Durable job dispatch | Move web-triggered execution to a durable queue with worker ownership, heartbeats, timeout/unknown-outcome handling and asynchronous status | Worker/process restart and redelivery tests without duplicate or abandoned operations |
| P1: multiworker scale | Shared control-plane state | PostgreSQL-backed checkpoints, approvals, runs, task records and native receipts; distributed leases/fencing; object storage for artifacts | Concurrent-writer, lease-expiry, failover and distributed-maintenance tests |
| P1: online migration | End-to-end CDC and cutover | Qualify the chosen native CDC engine, baseline/log boundary, ordering, deletes, schema changes, common cutoff and traffic switch | Continuous reconciliation and rehearsed cutover/recovery with application writes |
| P1: schema complexity | Richer conversion and procedure work | Versioned DDL conversion, indexes/FKs/defaults, engine-specific types, triggers, reviewed procedure translation and query equivalence | Golden schema/procedure suites and business-result equivalence tests on real engines |
| P1: operations | Observability and incident response | Structured events, traces, queue/worker metrics, progress estimates, alerts, failure categories and support runbooks | Run-to-log correlation, actionable alerts and rehearsed partial-failure response |
| P2: governance | Broader lineage and policy integration | Verified lineage through nested SQL/jobs, catalog synchronization, classification and owner approval workflows | Catalog entity resolution, source-to-target coverage and authorization-boundary tests |
| P2: economics | Measured cost and workload placement | Capture worker/cloud consumption, model tokens, storage/egress and operator effort; compare candidate destinations | Cost per validated GB/table and representative monthly total-cost estimates |
| P2: product breadth | Native cloud console controls | Cloud bundle authoring/review, stable native execution IDs, polling and evidence collection in the UI | The same bundle-hash, idempotency, unknown-outcome and role checks as the CLI |
| P2: service offering | Tenant isolation and quotas | Organization/workspace separation, scoped state/artifact stores, per-tenant credentials, quotas and billing boundaries | Cross-tenant denial tests and independently verified resource isolation |

P0 means required before a shared or live pilot in the relevant category. P1
items become necessary when the deployment promises long-running asynchronous
work, online migration or horizontal scaling. A bounded one-time migration
does not require every P2 product feature before it can be evaluated.

## What the current console deliberately does not imply

- A registered connection is a configured reference, not a credential vault or
  an automatic connectivity/compatibility certification.
- A planner's semantic suggestions are review material; they do not authorize
  generated code, schema rewrites or financial-data corrections.
- A completed native cloud job is not independent reconciliation or permission
  to switch application traffic.
- A retryable keyed snapshot is not a global database transaction, source-delete
  replication or automatic rollback of business writes.
- A local encrypted session cookie is not enterprise SSO or multi-tenant
  isolation. Backend role checks remain essential.
- A container build or architecture diagram is not a live deployment test.

## Suggested delivery sequence

### 1. Qualify one useful path

Choose a source/target combination that solves a real migration need, such as
PostgreSQL → Delta or SQL Server → Azure SQL through an approved ADF pipeline.
Define business keys, expected data types, source stability, acceptable outage,
validation invariants and the owner who can approve cutover. Exercise the
console and the CLI against the same reviewed contract.

### 2. Establish a shared pilot

Add enterprise identity, configure the quality gate and complete the private
single-host deployment. Rehearse checkpoint/artifact backup and recovery,
support rotation, token revocation and a deliberately failed migration. Keep
the pilot's advertised availability and data volume within measured limits.

### 3. Separate dispatch from web requests

Before promising large jobs, introduce durable dispatch and worker ownership.
Make cancellation cooperative and explicit about already committed writes.
Preserve one operation identity across web timeouts, retries and cloud-provider
unknown outcomes. Provide progress evidence from workers rather than synthetic
percentage estimates in the UI.

### 4. Scale or add CDC only when demanded

Implement shared state/fencing before using multiple control-plane instances.
For online migration, qualify snapshot/CDC consistency and the final change-log
position for the exact engines. Test the application's post-cutover behavior
and recovery from writes made on the target. Restoring a pre-migration backup
alone cannot safely undo those newer business writes.

### 5. Compare operating results

Measure validated throughput, failure/recovery behavior, analyst/operator time
and total cost against the existing ADF/Glue/Databricks or commercial-tool path.
Custom workflow control can be valuable; connector breadth, support and ongoing
maintenance still affect the decision. See [platform selection](platform-selection.md)
for the comparison criteria and cost inputs.
