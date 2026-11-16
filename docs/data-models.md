# OLTP, OLAP, star schemas, and non-relational data

OLTP and OLAP describe workload patterns; a star schema describes a data model.
Redis, PostgreSQL, Redshift, Cassandra, MongoDB, and Delta Lake describe different
engines or storage systems. These categories are not interchangeable connector
names. A supported source and target alone do not establish semantic equivalence
or an online migration guarantee.

Use [the connector matrix](connectors.md) for implemented read/write paths. Use
[the AWS](architecture/aws.md), [Azure](architecture/azure.md), and
[Databricks](architecture/databricks.md) architecture guides for managed execution.

## Workload planning

| Workload or model | Typical migration contract | Additional decisions to review |
| --- | --- | --- |
| OLTP | Preserve entities, stable keys, precise values, and application-visible constraints | Transaction boundaries, source write quiescence or a supported snapshot/CDC protocol, sequences, indexes, constraints, stored procedures, and cutover |
| OLAP | Preserve the declared analytical grain, measures, partitions, and history | Distribution, sorting, partition layout, aggregates, retention, query performance, and workload isolation |
| Existing star schema | Move reviewed dimensions and facts in dependency order with explicit keys | Fact grain, dimension membership, unknown-member policy, historical semantics, and cross-table validation |
| OLTP to new star schema | Execute an explicitly designed transformation job | Conformed dimensions, surrogate-key allocation, joins, SCD policy, fact aggregation, late arrivals, and source-to-target reconciliation |
| MongoDB document data | Preserve or explicitly project document shape and identity | Nested arrays/documents, BSON types, ObjectId representation, missing versus null, and update/delete handling |
| Cassandra wide-column data | Preserve the query-driven key and table design | Partition/clustering keys, tombstones, write timestamps, TTLs, consistency levels, and size of a partition |
| Redis key/value data | Use an explicit supported keyspace and data-type contract | Key identity, expiry, binary encoding, namespace, and whether the workload is a cache or a system of record |
| Delta Lake / lakehouse | Move or transform tables under a compatible Delta protocol | Managed versus external tables, storage credentials, schema changes, table features, retention, history, and catalog registration |

The local migration engine should be used within its documented snapshot and
type-conversion limits. Online replication, global cross-table consistency,
database behavior conversion, and live-application cutover require the separate
execution design described in the cloud guides.

## An existing star schema

Suppose the source already contains:

| Table | Grain | Key |
| --- | --- | --- |
| `dim_customer` | One row per agreed customer version | `customer_key` |
| `dim_product` | One row per agreed product version | `product_key` |
| `dim_date` | One row per calendar day | `date_key` |
| `fact_order_line` | One row per order line | `order_id`, `line_number` |

The specification can declare facts as dependent on their dimensions. Validate
that every applicable dimension reference resolves and compare measures such as
net quantity and net amount at the agreed cutoff. Loading the fact after the
dimensions controls execution order; it does not by itself prove referential
integrity or correct historical joins.

Use explicit sentinel/unknown members only when the owner has approved that
policy. Silently creating a member to make validation pass can hide missing data.

## Transforming OLTP into a star schema

A mapping from `customer.id` to `dim_customer.customer_key` is not sufficient
when historical dimension versions require several keys per natural customer.
Define these rules before generating a transformation job:

1. **Business grain:** what one fact row represents, including reversals and
   whether quantities/amounts are additive across time, currencies, or entities.
2. **Natural and surrogate keys:** how surrogate keys are allocated, persisted,
   and recovered on retry; how fact rows resolve a dimension version.
3. **Slowly changing dimensions:** Type 1 overwrite versus Type 2 history, which
   attributes trigger a new version, interval boundaries, and current-row rules.
4. **Time semantics:** event time versus ingestion time, time zones, late events,
   and effective-date joins.
5. **Deduplication:** deterministic business rules for conflicting records.
6. **Reconciliation:** accepted source rows and transformations that explain
   expected target counts and totals. A valid aggregation changes row counts.

Generate and run these transformations in an approved SQL/Spark/dbt job. The
local column mapping engine does not infer joins, construct SCD Type 2 tables,
or invent surrogate-key history. A complex transformation belongs in a reviewed
job with independent fixtures and reconciliation rules.

## Cross-model paths need an explicit projection

Examples of acceptable designs include:

- **MongoDB to PostgreSQL:** explicitly select top-level fields or normalize
  nested structures in a transformation job. Decide how arrays become child
  tables or JSON columns, and preserve document identity.
- **PostgreSQL to Redis:** choose a deterministic key namespace, supported Redis
  value type, expiry policy, and ownership of writes. A relational foreign key
  does not become an enforced Redis constraint.
- **Cassandra to Delta:** read the approved tables/partitions, preserve the agreed
  values, and define how TTL expiry and later deletions appear in the lakehouse.
- **OLTP to Redshift or Databricks:** use staging and a reviewed dimensional or
  analytical projection. The target warehouse's physical design and SQL behavior
  need their own validation.

The capability matrix may intentionally restrict particular paths or types.
Supported reads and writes should never be described as automatic support for
every source-to-target combination.

## Delta tables and their history

A row-based load into a new Delta table creates a new table history. It does not
preserve the source Delta transaction log, version numbers, change-data-feed
history, or catalog permissions. A physical table relocation or history-preserving
clone is a different operation with its own platform requirements.

Delta change data feed is an incremental input mechanism for an appropriately
configured table; enabling it does not itself provide an end-to-end cross-system
CDC protocol. Define starting version, retained history, application of deletes,
ordering, and restart checkpoints in the execution job. See the official
[Delta Lake overview](https://docs.delta.io/latest/index.html) and
[Databricks change data feed guide](https://docs.databricks.com/aws/en/delta/delta-change-data-feed).

## Semantic and governance outputs

Keep an evidence-backed business dictionary separate from the physical schema.
Store uncertain relationships as proposals, preserve source lineage, and record
which rule version produced the target. Publish reviewed table/column lineage to
OpenMetadata. Expose SQL views through Ontop when a semantic query interface is
needed. Use Unity Catalog and cloud/database permissions for the access paths
they actually govern. The setup steps are in [governance.md](governance.md).
