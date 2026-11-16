# Data quality, correction, reconciliation and lineage

## Implemented snapshot behavior

The local LangGraph workflow performs source discovery, metadata-only semantic advice when configured, plan construction, approval, staging, keyed load and deterministic reconciliation. Every selected source is staged before the first destination write. Source quiescence or an immutable snapshot is an operator prerequisite; the boolean policy records that assertion, it does not pause an external database.

A table mapping can specify `quality_rules` on target column names. Available checks are `not_null`, `non_blank`, `unique`, `regex`, `range`, and `allowed_values`. Numeric bounds use quoted decimal strings, avoiding binary-float interpretation. Regex checks have a per-evaluation timeout. A unique rule also requires a non-null value.

Uniqueness, migration keys and reconciliation share scalar equality: Decimal
scale does not change numeric equality, signed floating-point zero compares
equally, and timezone-aware timestamps compare as UTC instants. Naive wall-clock
timestamps stay distinct from instants. Text comparison is case-sensitive unless
an approved transformation changes it; destination-specific collations need
separate review. Allowed values are interpreted through the declared target
type, so quoted `"1.00"` can define an exact Decimal value.

```yaml
quality_rules:
  - name: valid-email
    column: email
    check: regex
    pattern: '[^@\s]+@[^@\s]+\.[^@\s]+'
    correction: lower
  - name: unique-email
    column: email
    check: unique
  - name: nonnegative-balance
    column: balance
    check: range
    minimum: '0'
```

Corrections are explicit `trim`, `lower`, or `upper` operations. They are part of the plan hash the operator approves. All corrections run before all checks so later normalization cannot invalidate an earlier uniqueness check unnoticed. Automatic quality corrections cannot modify migration keys. Column mappings also support the small audited transform vocabulary documented in the README; arbitrary model-generated Python/SQL is never evaluated by the snapshot worker.

No probabilistic decision can accept a failed balance, invent missing data, or merge two customer identities. Such business changes require a revised mapping or separately tested transformation job and fresh approval. SCD2, fuzzy deduplication, entity resolution, address verification, and domain-specific repair algorithms are extension work.

## Evidence and failures

Each run retains:

- `plan.json`: exact mappings, source-to-target column lineage, quality rules and hash.
- `catalog.json`: selected schemas and bounded null-count profiles; no raw row samples.
- `advice.json`: optional model hypotheses and owner questions, explicitly advisory.
- `staging/*.sqlite`: private replayable transformed data. These files contain business data and need encrypted local disks, backup controls, and a retention policy.
- `staging/*.json`: row counts, content digest, transformation counts and quality results.
- `validation.json`: full mapped-column comparison, missing/extra/duplicate keys, mismatch counts, and correction counts.
- `quality_failure.json`: first failing table, row ordinal and rule/column message without raw values.
- Checkpoints, approval receipts, and committed-batch receipts in separate local SQLite stores.

A quality failure stops before target data writes. A connector or post-load reconciliation failure can leave committed target batches; those are retained for investigation and controlled replay. There is no automatic destructive rollback. A retry reuses staged data and stable batch receipts, with keyed upserts protecting an acknowledgement-loss replay.

`target_scope: entire_table` requires exact key coverage. `mapped_keys` validates all migrated keys while reporting extra destination keys as outside scope. Neither mode propagates source deletes. Full-copy equality is not equivalent to business-rule completeness: define relevant rules and validate financial/domain invariants in a reviewed external job where required.

## Lineage and governance

Plan lineage records physical/logical source and target table names, each mapped field, transformations and quality rules. The OpenMetadata helper publishes table/column lineage after resolving actual catalog entities; it needs explicit publication approval. Group qualified discovery names by their real schema before publishing. Query-defined sources retain query metadata; complete lineage through arbitrary stored procedures is not automatically inferred.

Ontop exports read-only R2RML/OWL mappings over approved SQL-facing target tables. It supplies business vocabulary and SPARQL access; it does not move data or enforce destination access. Native database privileges and Unity Catalog remain the enforcement boundary.

## Native cloud jobs

The native cloud graph ends at `EXECUTION_SUCCEEDED_REQUIRES_RECONCILIATION`. A provider's job-success flag is not a data-quality certificate. Shipped Azure and Databricks templates support only their documented transformations; unsupported local quality rules must be rejected or implemented in a reviewed worker. The AWS Glue contract requires an existing worker to validate the immutable manifest and enforce its declared semantics.

Online migration additionally needs engine-specific snapshot/CDC coordination, ordering and delete handling, a common final log position, final reconciliation and a rehearsed cutover. Returning traffic after the target has accepted new writes requires reconciliation or reverse replication; restoring an old snapshot alone can discard those writes.
