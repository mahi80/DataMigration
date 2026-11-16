# Platform, database and cost choices

Choose the destination from the application's access pattern and operational
requirements. The migration agent coordinates that decision and records its
evidence; it does not make incompatible database semantics disappear. The
recommendations below are architecture judgments, not performance benchmarks or
price quotes. Verify current regional prices when sizing a deployment.

## Which database should we use?

| Need | Starting choice | AWS | Azure | Main tradeoff |
| --- | --- | --- | --- | --- |
| Tool development and a single-host pilot | SQLite control state on a durable local disk | EC2 + encrypted EBS | VM + encrypted managed disk | Small operational footprint; no horizontal control-plane scaling |
| Transactional application with joins, constraints and general SQL | PostgreSQL | RDS PostgreSQL; evaluate Aurora PostgreSQL when its operational model helps | Azure Database for PostgreSQL Flexible Server | Size for write/read load, indexes, HA, storage and connection count |
| Existing SQL Server application with substantial T-SQL logic | Preserve SQL Server semantics initially | Managed/self-managed SQL Server appropriate to compatibility needs | Azure SQL Database or Managed Instance after feature assessment | Engine compatibility may outweigh nominal compute savings |
| Large BI scans and a dimensional warehouse concentrated in AWS | Redshift | Provisioned or Serverless | Cross-cloud only when the business case pays for movement | Query concurrency, active compute, storage and cross-region egress matter |
| Batch analytics, durable history and an open table format | Delta Lake on object storage with scheduled compute | S3 + qualified Spark/Databricks jobs | ADLS Gen2 + qualified Spark/Databricks jobs | Storage and compute separate; table layout, compaction and job operations still matter |
| Existing lakehouse, notebooks, ML and enterprise catalog investment | Databricks + Delta + Unity Catalog | Databricks on AWS | Azure Databricks | Existing skills/governance may justify platform charges; avoid idle compute |
| Aggregate-oriented documents with BSON semantics | MongoDB | A suitable managed or self-managed MongoDB deployment | A suitable managed or self-managed MongoDB deployment | Relational flattening is a business-model change, not a simple connector option |
| Cache, short-lived lookup or shared ephemeral state | Redis | Appropriate managed Redis-compatible service | Appropriate managed Redis-compatible service | Memory/HA cost and expiry semantics; do not treat a cache export as a relational backup |
| Existing distributed wide-column workload designed around partition keys | Cassandra | Qualified Cassandra deployment | Qualified Cassandra deployment | Requires data-model expertise and capacity planning; unnecessary complexity for ordinary relational CRUD |

Managed services with a compatible API are **not automatically interchangeable**
with the upstream database. Test engine/version-specific features and restrictions
before using this repository's adapter. The current Cassandra and Redis adapters
are deliberately narrower than the full database products.

For this repository's pilot, use SQLite for control state and your existing
business database as the migration endpoint. For a new general-purpose OLTP
application, PostgreSQL is a reasonable starting candidate. For a new analytics
destination, compare Redshift with a Delta lakehouse using representative joins,
scans, concurrency and job windows. Keep storage and compute in the same region
as the main workload where practical.

The future multiworker control plane should use a transactional shared store,
distributed ownership/fencing, durable job delivery and external artifact
storage. PostgreSQL is a reasonable candidate, but this release does **not**
implement that distributed control plane. Redis alone should not be the only
durable approval or migration receipt record.

## Cost model

Use measured workload quantities in a regional calculator. A useful monthly
estimate is:

```text
Total cost = control-plane compute + control-state disk/backups
           + source/target database capacity and I/O
           + migration worker hours / native job consumption
           + object storage + requests + version retention
           + network egress / private networking
           + model tokens or provisioned model capacity
           + catalog, observability, CI and SonarQube operation
           + engineering, support and incident recovery
```

Collect these inputs before choosing a service:

| Input | Why it changes the answer |
| --- | --- |
| Region, currency and existing commitments | Public list prices are not your negotiated/regional total |
| Source size, table count, LOB size and row width | Determines scan, staging, transfer and verification work |
| One-time snapshot versus continuous change rate | Changes worker duty cycle, log retention and cutover design |
| Daily active compute and concurrency | Determines whether burst/serverless or steady capacity is economical |
| Required outage, RPO/RTO and HA | Adds redundancy, replication and operational requirements |
| Cross-cloud/region movement | Adds egress, latency and duplicated storage |
| Verification coverage and retention | Full reconciliation and retained snapshots consume resources too |
| Existing platform contracts and staff skills | Reusing a governed platform can cost less than operating a new stack |

RDS PostgreSQL prices depend on database capacity, storage and other selected
features; consult the [RDS PostgreSQL pricing page](https://aws.amazon.com/rds/postgresql/pricing/).
Redshift separates compute choices from managed storage, and offers provisioned
and serverless billing models; consult [Redshift pricing](https://aws.amazon.com/redshift/pricing/).
Azure's managed PostgreSQL tiers and Databricks compute offerings have their own
regional meters: [PostgreSQL Flexible Server pricing](https://azure.microsoft.com/en-us/pricing/details/postgresql/flexible-server/)
and [Azure Databricks pricing](https://azure.microsoft.com/en-us/pricing/details/databricks/).

No fixed dollar or rupee saving is claimed here. A small, intermittent pilot and
a continuously replicated multi-terabyte warehouse have different cost drivers.

### Practical cost controls

- Keep the advisor disabled for deterministic mappings; when enabled, send
  bounded metadata and cap tokens. Do not ask a model to transform every row.
- Use the local engine for bounded snapshots and testing. Use qualified native
  bulk workers for large transfers; Python row-level upserts are not a universal
  throughput solution.
- Schedule batch compute and stop idle workers where the platform permits it.
- Reuse an existing governed Databricks, ADF or Glue estate when it fits the
  migration. Avoid introducing another always-on service merely for an agent demo.
- Retain the staging and receipts required for recovery, then apply an explicit
  retention policy after the migration is closed and retries are no longer valid.
- Measure cost per validated GB/table plus operator hours, failure rate and
  recovery time. A cheap copy that requires extensive manual repair is expensive.

## How this compares with Informatica and other paid tools

Informatica already markets integration, AI-assisted operations, data quality,
catalog, lineage, governance and master-data capabilities in IDMC. Its current
materials also describe CLAIRE and agentic capabilities. It would be misleading
to claim that adding an LLM, a catalog or quality rules creates capabilities those
products lack. See the official [IDMC platform](https://www.informatica.com/platform.html),
[data quality](https://www.informatica.com/products/data-quality.html), and
[data catalog](https://www.informatica.com/products/data-catalog.html) descriptions.

| Decision factor | Potential advantage of this custom workbench | What a commercial platform can offer / what must be proved here |
| --- | --- | --- |
| Exact domain workflow | Control graph states, approval evidence, typed rules and native-job contracts in code | You own usability, operations, upgrades and integration maintenance |
| Model/provider choice | Optional metadata advisor can use approved endpoints; no per-row model dependency | Choice alone does not establish better suggestions or lower total cost |
| Native cloud reuse | Coordinate existing Glue/DMS/ADF/Databricks jobs | Qualified commercial connectors may reduce engineering and support work |
| Agent interoperability | Small explicit MCP and A2A interfaces with separate approval | This release supports a limited protocol surface and a single workspace |
| Governance customization | Reviewable mapping/lineage artifacts and Ontop/OpenMetadata hooks | Broad automated lineage, policy administration and enterprise workflows require further work |
| License/subscription spend | No vendor migration-platform subscription is introduced by this code | Hosting, dependencies, operations, engineering and support are not free |
| Database breadth | Add a narrowly specified adapter for a particular use case | Mature products may support more engines, edge types, CDC and vendor-specific logic |
| Reliability and support | Inspect and test exact replay and reconciliation behavior | No production SLA, managed upgrades, customer-scale benchmark or service certification is supplied |

This workbench can be a better fit when the migration portfolio is bounded, the
team already operates the relevant cloud services, and domain-specific approvals
or transformations justify owning the implementation. A commercial platform can
be a better fit when broad connector coverage, managed CDC, enterprise support,
no-code operations or a contracted SLA dominates the decision. A hybrid is often
reasonable: let the agent prepare and govern a plan while an established platform
executes the data movement.

## Evaluation before a production decision

Use the same representative workload to evaluate both choices. Include large
tables, empty tables, nulls, Decimal precision, timezone differences, composite
keys, LOBs, deletes, schema drift and the actual procedure/query estate. Measure
full validation, restart after an acknowledged/unknown write, cutover behavior,
operator effort and monthly total cost. Record unsupported features rather than
excluding them from the benchmark. Do not infer production suitability from the
small synthetic demo.

## Public inspiration and limits

Turgon's public [framework article](https://www.turgon.ai/post/langchain-langgraph-or-custom-choosing-the-right-agentic-framework)
describes a progression from LangChain to LangGraph and custom components for
some requirements. That is useful design context. The public material does not
provide its proprietary implementation, a complete connector certification
matrix, or evidence that this independently built repository is equivalent to
Turgon's commercial tooling.
