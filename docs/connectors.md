# Database and storage support

This document describes the code in this repository, rather than the theoretical
connector coverage of AWS, Azure, Databricks, or a vendor catalog. All direct
connectors implement **snapshot reads and keyed upserts**. None implements CDC,
automatic application cutover, automatic source-delete propagation, or migration
of an entire database server's users, grants, triggers, extensions and procedures.

The local engine requires an explicitly quiesced source and exclusive ownership
of the target tables while a migration runs. Delta additionally pins each source
table to a Delta version. This is not a consistent snapshot across multiple Delta
tables. Do not vacuum away a pinned version before migration and validation end.

## Implemented matrix

| Kind | Source | Target | Implementation and limits | Verification in this repository |
|---|---|---|---|---|
| `csv` | Yes | Yes | Explicit schema; local root; exact null/base64/ISO representations; bounded atomic keyed replacement | Real local typed values, replay and failure-preserves-original tests |
| `jsonl` | Yes | Yes | Explicit schema or generated sidecar; typed value envelope; bounded local keyed replacement | Real local Decimal, datetime, binary, JSON and replay tests |
| `parquet` | Yes | Yes | Single local Parquet file per table; scalar Arrow types; bounded atomic replacement | Real local Decimal, timestamp, schema and replay tests |
| `sqlite` | Yes | Yes | SQLAlchemy; new target tables or an existing table with enforced keys; exact Decimal writes rejected | Real local typed-data, replay, key and timezone tests |
| `postgresql` | Yes | Yes | SQLAlchemy + psycopg; new target tables or existing unique keys; `ON CONFLICT` upserts | Shared SQL code tested on SQLite; no live PostgreSQL run |
| `mysql` | Yes | Yes | SQLAlchemy + PyMySQL; target schema and enforced keys must already exist | Implemented; requires service integration testing |
| `sqlserver` | Yes | Yes | SQLAlchemy + ODBC; target schema and enforced keys must already exist | Implemented; requires service integration testing |
| `mongodb` | Yes | Yes | Ordered BSON Extended JSON string envelope; replacement by `_id`; explicit database | Real BSON serialization with a fake target; no live MongoDB run |
| `redis` | Yes | Yes | Redis 7+ hashes in a restricted prefix; preserves binary fields/values and absolute key expiry; per-field expiry rejected | Fake-service binary/expiry/replay/namespace tests |
| `cassandra` | Yes | Yes | Existing scalar tables; full partition and clustering key; TTL, counters, UDTs and collections rejected | Adapter TTL rejection test; no live cluster run |
| `redshift` | Yes | Yes | Existing target; bounded temporary staging table and `MERGE`; single writer required | SQL binding and rollback contract tests; no live Redshift run |
| `databricks` | Yes | Yes | SQL warehouse; scalar columns; existing Delta target with explicit business keys | Unity Catalog identifier and parameter-binding contract tests; no live workspace run |
| `delta` | Yes | Yes | delta-rs; local/object-store table roots; scalar columns, Decimal and keyed `MERGE`; schema evolution disabled | Real local version, Decimal, replay, duplicate-key and timezone tests |

“Implemented” does not mean every source/target pair is certified. A successful
migration also needs an explicit, compatible data model, type mapping, stable
business key, reachable endpoint and sufficient permissions. Network connectors
must pass a representative integration run in your environment before production.

## OLTP, OLAP and star schemas

OLTP and OLAP describe workload characteristics. A star schema describes a data
model. They are not interchangeable connector types.

- **OLTP table migration:** retain identifiers and exact types; specify table
  dependencies; separately provision and verify constraints, indexes and business
  logic that the row-copy tool does not recreate.
- **OLAP migration:** Redshift and Databricks adapters support bounded keyed table
  transfers. Large loads should use native bulk pipelines such as S3 COPY or
  Databricks Jobs, with the orchestration adapters in `src/datamigration/cloud`.
- **Existing star schema:** migrate dimensions before facts using `depends_on`.
  Preserve surrogate keys where the plan requires them. The tool does not infer
  fact grain, generate SCD Type 2 history, or decide late-arriving dimension rules.
- **Creating a new star schema:** define grain, dimensions, measures and history
  policies as reviewed transformation artifacts. Execute the business-specific
  SQL/Spark pipeline and reconcile its expected results. A flat table copy is not
  a substitute for that modeling work.
- **Document, key/value and wide-column stores:** use the explicit representations
  below. Automatic semantic conversion to a relational or star schema is not
  claimed.

## Connection configuration

Connection strings are read from environment variables. Do not put passwords,
tokens, SAS strings or access keys into YAML options or committed examples.
Install the extras needed by your selected endpoints, for example:

```bash
pip install -e '.[postgres,mongo,redis,cassandra,redshift,delta,databricks]'
```

Only install the drivers you use. SQLAlchemy is part of the base installation.
SQL Server also needs a compatible Microsoft ODBC driver installed on the host.

### SQLite and PostgreSQL

```yaml
source:
  kind: postgresql
  url_env: SOURCE_DATABASE_URL
  options:
    schema: public
target:
  kind: postgresql
  url_env: TARGET_DATABASE_URL
  options:
    schema: migration
```

Use a URI such as `postgresql+psycopg://user:password@host:5432/database` in the
environment variable. PostgreSQL schema namespaces must already exist. SQLite
URIs look like `sqlite:////absolute/path/source.db`.

The adapter supports integer, float, Decimal, string, boolean, date, datetime,
JSON and binary type families. Unsupported native types fail during inspection;
use a deliberately typed source view or explicit mapping for arrays, ranges,
custom domains and other engine-specific types. Automatic DDL creates the mapped
columns and specified primary key, not a complete replica of source DDL.

Use `mysql+pymysql://...` for MySQL and an appropriate `mssql+pyodbc://...` SQLAlchemy
URI for SQL Server. `azure_sql` is an alias for the same SQL Server/ODBC route.
Those target tables must be provisioned first.

**Precision and time:** SQLite storage cannot preserve arbitrary Decimal values;
map to exact integer units or an explicit text representation instead. Decimal
precision and scale should be specified in mappings. SQL and Delta writes reject
aware/naive datetime mismatches; an approved conversion must establish whether a
timestamp represents an instant or a local wall-clock value.

### MongoDB

Specify a database in the MongoDB URI or `options.database`. Collections are
exposed as tables. A document is represented as:

```json
{
  "_id": "{\"$oid\":\"507f1f77bcf86cd799439011\"}",
  "document": "{\"_id\":{\"$oid\":\"507f1f77bcf86cd799439011\"},\"amount\":{\"$numberDecimal\":\"120.50\"}}"
}
```

Both columns are opaque strings containing canonical BSON Extended JSON, with
field order retained. This preserves ObjectId, Decimal128, binary, dates, nested
structures and document-valued `_id` ordering. Parsing the envelope to an ordinary
JSON object would allow sorting of meaningful BSON field order; the direct
MongoDB adapter therefore requires string columns. The target checks that the envelope key matches the
document `_id`, then uses `replace_one(..., upsert=True)`. Unacknowledged `w=0`
targets are rejected, and each replacement must be acknowledged before a batch
is reported complete. It does not copy indexes,
validators, change streams, transactions or collection options. MongoDB targets
accept this envelope, not an arbitrary relational row. To extract business fields
or convert to relational JSON, create a reviewed normalization mapping/job that
explicitly acknowledges the change in BSON semantics.

### Redis

```yaml
source:
  kind: redis
  url_env: SOURCE_REDIS_URL
  options:
    pattern: 'migration:customers:*'
    table: hashes
```

The pattern must be a nonempty literal prefix followed by `*`; scanning all keys
with `*` is rejected. Source and target configurations each restrict their own
namespace. Use `_key` as the target business key.

Rows contain `_key`, `payload` and `_expires_at_ms`. Keys must be UTF-8. Payloads
encode every hash field and value as base64, retaining arbitrary bytes. Expiry is
an absolute Unix millisecond timestamp, or `-1` for a persistent key. Writes
atomically replace the selected hash and apply `PEXPIREAT`, so retries do not
extend its life. Source keys must remain valid throughout staging and validation;
expiring keys fail rather than disappear silently.

The adapter requires Redis 7+ for `PEXPIRETIME`. On Redis 7.4+, it checks
`HPEXPIRETIME` and rejects per-field expiration. Strings, streams, lists, sets,
sorted sets, modules, Redis Cluster/Sentinel discovery and whole-instance backup
restores are outside this adapter. Required commands include INFO, SCAN, TYPE,
HGETALL, PEXPIRETIME, and EVAL; 7.4+ source reads also require HPEXPIRETIME.

### Cassandra

Use `cassandra://user:password@host:9042/keyspace`, or provide `options.keyspace`.
`options.contact_points`, `options.tls` and `options.ca_file` can configure network
discovery and TLS. The adapter never invents a partition key or creates a table.

Every target key must match the complete existing partition/clustering key. Reads
page at LOCAL_QUORUM and writes use LOCAL_QUORUM. Native CQL timestamps are
interpreted as UTC epoch instants and exposed as aware UTC datetimes; incoming
naive values require an explicit reviewed conversion. UUID/timeuuid values use a documented string representation
and are reconstructed for CQL writes. Scalar data only is supported. Table-level
and per-cell TTLs are rejected; write timestamps, tombstones, counters, UDTs and
collection semantics are not replicated. This is not a Cassandra cluster upgrade
or SSTable migration tool.

### Redshift

Use `redshift://user:password@host:5439/database`; `options.schema` defaults to
`public`. Targets must exist with the expected columns. The adapter checks target
key uniqueness, creates a session-temporary staging table per bounded batch,
binds values into it, performs `MERGE`, removes the temporary table and commits.
A failed batch rolls back. It does not issue `ON CONFLICT`, drop permanent tables,
or assume that Redshift enforces declared primary keys.

Keep one writer per target during the migration. Large-table performance,
distribution/sort keys, native COPY/UNLOAD, IAM database authentication and WLM
configuration require the native cloud execution path or additional integration.
The SQL session is set to UTC. TIMESTAMPTZ is exposed as aware UTC datetimes;
TIMESTAMP without time zone retains naive wall-clock semantics. Native timestamp
types and timezone metadata participate in schema drift detection.

### Databricks SQL and Unity Catalog

```yaml
target:
  kind: databricks
  url_env: DATABRICKS_HOST
  options:
    http_path: /sql/1.0/warehouses/REPLACE_WITH_WAREHOUSE_ID
    token_env: DATABRICKS_TOKEN
    catalog: main
    schema: migration
```

The host environment variable contains the workspace HTTPS URL. The token is a
separate environment variable. Three-part `catalog.schema.table` identifiers are
supported; short names use the configured catalog/schema. Targets must already
exist and use Delta format. The adapter validates key uniqueness and submits
parameterized `MERGE` statements. Keep one writer per target while using this
bounded adapter.

The SQL session uses UTC. TIMESTAMP/TIMESTAMP_LTZ values represent instants and
are exposed as aware UTC datetimes; TIMESTAMP_NTZ remains a naive wall-clock
value. The adapter rejects writes that silently cross these meanings.

Unity Catalog remains authoritative for access control. This code does not create
catalogs, grant privileges or bypass their policies. Complex STRUCT/ARRAY/MAP/
VARIANT columns need an explicit projection or a reviewed Spark job. Databricks
Jobs execution is available through the separate cloud adapter. Mosaic AI model
serving is an optional agent endpoint, not the component that moves table rows.

### Delta Lake

```yaml
target:
  kind: delta
  url_env: TARGET_LAKE_ROOT
  options: {}
```

`TARGET_LAKE_ROOT=/absolute/path/lake` means table `customers` lives at
`/absolute/path/lake/customers`. Local tables are discovered by their `_delta_log`
folders. For remote roots, supply `options.tables` explicitly and configure the
object store through delta-rs environment/identity mechanisms. Secret-bearing
`storage_options` are rejected.

An empty table can be initialized from the approved schema. Updates use Delta
MERGE, with source key validation and existing-target uniqueness checks. Automatic
schema evolution is disabled. Decimal precision must be 1–38 with an explicit
scale. JSON/nested values are rejected unless an approved mapping serializes them
to strings or a Spark pipeline handles the intended nested schema.

This adapter does not enable or consume Change Data Feed, perform OPTIMIZE/ZORDER,
vacuum old versions, or replicate Databricks-specific table features. Check the
delta-rs reader/writer protocol compatibility against the actual table before use.
Unity Catalog managed tables should use authorized Databricks execution paths;
an object-store URI is not a substitute for table authorization.

## Flat files: CSV, JSON Lines and Parquet

These use the same typed table/column contract and durable staging engine as the
database adapters. Relational access uses SQLAlchemy reflection/Core; a CSV file
itself is not an ORM database. Each connection points to a **local directory**,
and logical table `customers` means `customers.csv`, `customers.jsonl`, or
`customers.parquet` according to the selected kind.

```yaml
source:
  kind: csv
  url_env: SOURCE_FILES_ROOT
  options:
    csv_null_token: '\N'
    max_rows: 100000
    schemas:
      customers:
        columns:
          - {name: customer_id, data_type: string, nullable: false}
          - {name: name, data_type: string}
          - {name: amount, data_type: decimal, precision: 18, scale: 2}
          - {name: created_at, data_type: datetime, timezone: UTC}
        primary_key: [customer_id]
target:
  kind: parquet
  url_env: TARGET_FILES_ROOT
  options:
    max_rows: 100000
```

Set `SOURCE_FILES_ROOT` and `TARGET_FILES_ROOT` to absolute local directories.
`options.schemas.<table>` can also be a list of column objects; in that form,
`options.keys.<table>` can document its source key. Every migration mapping must
still specify target `keys` explicitly. Table names are simple filename stems.
Directory traversal, table symlinks and remote URI writes are rejected.
The effective typed/plain codec and CSV null token are included in source schema
signatures; changing their interpretation after approval requires a new plan.

### Source formats

- **CSV:** types are never inferred from string samples. Supply an explicit
  schema or a sidecar generated by this tool. The CSV header must match the schema
  exactly. Strings such as `001` remain strings; empty text remains empty text.
  `\N` denotes null by default. Decimals use exact decimal text, dates/timestamps
  use ISO text, binary uses base64, and booleans use explicit true/false or 1/0.
  A literal value equal to the reserved null token must use a different reviewed
  token. Output writing rejects such collisions rather than losing the value.
- **JSON Lines:** each nonempty line is a row object and must match the declared
  schema. An explicit schema is required unless the tool created the sidecar.
  Duplicate object keys are rejected. Exact Decimal, datetime, date, binary and
  nested values in generated files use the tool's reversible typed codec; the
  sidecar distinguishes that codec from ordinary input JSON. This is not a MongoDB
  dump format. MongoDB's BSON envelope remains an opaque string when exported.
- **Parquet:** source field types come from Arrow metadata when no explicit
  schema is supplied. Scalars, dates, microsecond-or-coarser timestamps, binary
  and decimals are supported. Nested STRUCT/LIST/MAP values and nanosecond
  timestamps require a reviewed projection or conversion. One file represents one
  table; partitioned data lakes should use Delta or an appropriate native worker.

### Target behavior and bounds

Each new file gets a sibling `<table>.<format>.schema.json` manifest. Keep it with
the file: it records schema, keys, precision/timezone information and typed value
representations. The adapter writes an empty table only after its schema has been
validated. Existing unmanifested files are not overwritten; migrate to a new table
first. Changing target keys or the schema contract requires a new reviewed target.

For every incoming batch, the adapter loads the existing bounded target, checks
its keys, performs an upsert by the approved keys, writes a temporary file, flushes
it, and atomically replaces the data file. Retrying the same batch does not append
duplicate rows. A conversion/budget failure before replacement leaves the previous
data file intact. Schema initialization and data files are separate artifacts;
interrupted initialization may require operator cleanup. Atomic replacement is a
local-filesystem contract, not an object-store transaction guarantee.

The default bound is 100,000 rows per file. Rewriting a whole file for every batch
is intentionally for bounded transfers and demonstrations, not warehouse-scale
ingestion. Use larger reviewed batches for small files, and use Delta/Spark/native
bulk execution for large datasets. No source-delete propagation is implied.

### Run the synthetic file example

From the repository root, after installing the package:

```bash
python -m pip install -e '.[files]'
python examples/flatfiles/run_example.py --directory .demo/typed-files
```

This creates two synthetic CSV rows, a complete migration YAML file, the approved
plan and run receipts, and a Parquet target with its schema sidecar. It checks the
same reconciliation flow as a normal migration, including the string key `001`,
exact decimal amounts, UTC timestamps and nulls. The directory must be new. The
example approves only the synthetic data that it just created; for real files,
use `datamigrate run` followed by review and `datamigrate approve`.

## Table, view and stored procedure discovery

`inventory_sql(connector, include_definitions=False)` in
`connectors/sql_inventory.py` uses read-only SQLAlchemy/native catalog queries.
It reports accessible schemas, table names, views and registered reviewed-query
sources. Set `options.inventory_schemas` to constrain discovery and
`options.inventory_max_assets` to bound the result (default 1,000). Truncation is
explicitly reported.

Stored procedure inventory is implemented for PostgreSQL, SQL Server and MySQL.
Definition retrieval is opt-in and subject to the database principal's metadata
permissions; unavailable/encrypted bodies may be null. The tool **does not execute,
automatically translate, recreate or schedule procedures**. Review side effects,
transactions, temp tables, dynamic SQL, vendor-specific functions and application
dependencies before implementing a target procedure. SQLite reports that it has
no supported stored procedure inventory.

Existing SQL views can be named as migration sources. Put selected view names in
`options.query_views` to include them in the connector's source listing. View
targets are rejected. Use source credentials enforced as read-only by the database.
Relational discovery uses qualified names from `options.schema` or the driver's
default schema, such as `public.customers` or `dbo.customers`. SQLite keeps bare
names unless a schema is explicitly configured. Registered query sources retain
their logical names. Use these discovered names in table scopes and mappings.

## Reviewed nested queries and CTE sources

Install the `queries` extra. A logical query source has SQL text, named bind values,
an explicit result schema and stable keys:

```yaml
source:
  kind: postgresql
  url_env: SOURCE_READONLY_DATABASE_URL
  options:
    read_only_queries: true
    queries:
      high_value_customers:
        sql: |
          WITH totals AS (
            SELECT customer_id, SUM(amount) AS total
            FROM orders
            GROUP BY customer_id
          )
          SELECT customer_id, total
          FROM totals
          WHERE total >= :minimum
        parameters: {minimum: 1000}
        columns:
          - {name: customer_id, data_type: integer, nullable: false}
          - {name: total, data_type: decimal, precision: 20, scale: 2}
        keys: [customer_id]
tables:
  - source: high_value_customers
    target: customer_totals
    keys: [customer_id]
```

The logical source is discoverable and uses the same plan, approval, staging and
reconciliation flow as a physical table. Parameters are bound through SQLAlchemy;
they are not interpolated into SQL. The returned columns must match the declaration,
and duplicate keys are rejected by the staging engine. Query definition and bind
values participate in the approved plan/signature.

SQLGlot restricts input to one SELECT/CTE/set query. DML, DDL, SELECT INTO, locking,
administrative statements and unrecognized/user-defined function calls are
rejected. **Parsing is not a SQL security sandbox.** Use a database-enforced
read-only principal and appropriate statement/resource limits. PostgreSQL/MySQL
query sessions additionally request a read-only transaction; SQLite uses query-only
mode. SQL Server requires database/role-level read-only enforcement. A reviewed
view is the preferred route for complex vendor-specific logic that the parser
does not support. Read budgets limit fetched rows; they do not estimate or cap the
database's cost of evaluating a large join or aggregation.

### Run the synthetic nested-query example

```bash
python -m pip install -e '.[queries]'
python examples/queries/run_example.py --directory .demo/reviewed-query
```

The script creates a fresh SQLite source containing three synthetic orders and
one view. A parameterized CTE aggregates order amounts in integer cents and
selects the customer whose total exceeds the declared minimum. It writes that
result to a separate keyed SQLite table, reconciles it, and saves the full
`migration.yaml` and `inventory.json`. SQLite query-only mode is enabled for query
execution. PostgreSQL, SQL Server and MySQL deployments must use credentials with
database-enforced read-only access. Procedure inventory is metadata only.

To discover a real connection without running a migration, use
`datamigrate discover migration.yaml --side source` or `--side target`.
For relational views and procedure metadata, use
`datamigrate inspect-sql migration.yaml --side source`;
add `--include-definitions` only when the definitions should appear in the output.

## Official interface references

- [Redshift MERGE](https://docs.aws.amazon.com/redshift/latest/dg/r_MERGE.html)
- [Redshift staging tables](https://docs.aws.amazon.com/redshift/latest/dg/merge-create-staging-table.html)
- [Databricks SQL connector](https://docs.databricks.com/aws/en/dev-tools/python-sql-connector)
- [DeltaTable API and MERGE](https://delta-io.github.io/delta-rs/latest/api/delta_table/)
- [Delta writes and schema handling](https://delta-io.github.io/delta-rs/latest/usage/writing/)
- [Redis PEXPIRETIME](https://redis.io/docs/latest/commands/PEXPIRETIME/)
- [Redis HPEXPIRETIME](https://redis.io/docs/latest/commands/HPEXPIRETIME/)
