# Cloud examples

These examples never deploy infrastructure during compilation. Submitting a run
is a separate action that can incur charges and write target data. Replace every
placeholder, provision the required services and review the complete plan first.

| File | Scope |
|---|---|
| `aws-glue.json` | Existing approved Glue job orchestrated by Step Functions Standard |
| `aws-dms.json` | Existing full-load-and-CDC task; preflight review gate is deliberately false |
| `azure-copy.json` | SQL Server/Azure SQL → Azure SQL snapshot upsert |
| `databricks-delta.json` | Existing Databricks job; the provided Spark example uses pinned Delta table versions |

## Compile without cloud access

Install the package from the repository root, then run:

```bash
python examples/cloud/compile_and_inspect.py aws examples/cloud/aws-glue.json output/aws
python examples/cloud/compile_and_inspect.py azure examples/cloud/azure-copy.json output/azure
python examples/cloud/compile_and_inspect.py databricks examples/cloud/databricks-delta.json output/databricks
```

Each output folder contains a complete bundle and separate deployment artifacts.
The DMS example fails compilation until a real source/target preflight is reviewed.
Do not set the review flag merely to make an example compile.

## LangGraph native execution flow

Use the root CLI where available, or the direct API below. Keep checkpoint files
and `.migration/cloud-submissions.sqlite3` durable. Use the same thread ID,
operation ID and executor configuration when resuming after a process restart.

```python
import json
from pathlib import Path
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command
from datamigration.cloud.workflow import build_cloud_graph

Path('.migration').mkdir(exist_ok=True)
spec = json.loads(Path('examples/cloud/aws-glue.json').read_text())
config = {'configurable': {'thread_id': 'approved-migration-001'}}
executor_config = {'region': 'us-east-1'}

with SqliteSaver.from_conn_string('.migration/cloud-checkpoints.sqlite3') as saver:
    graph = build_cloud_graph(saver, 'aws', executor_config)
    state = graph.invoke({'spec': spec, 'operation_id': 'approved-migration-001'}, config)
    # Inspect state['bundle'], preconditions and the exact bundle_hash.
    # Stop here until a named authorized reviewer approves that concrete bundle.
```

After the external resources are provisioned and that bundle has been reviewed:

```python
with SqliteSaver.from_conn_string('.migration/cloud-checkpoints.sqlite3') as saver:
    graph = build_cloud_graph(saver, 'aws', executor_config)
    state = graph.get_state(config).values
    state = graph.invoke(Command(resume={
        'approve': True,
        'reviewer': 'authorized-reviewer@example.com',
        'bundle_hash': state['bundle_hash'],
    }), config)
    # Returns at the status-wait interrupt after submitting the native job.
```

For each status check, reconstruct the graph with the same bindings and invoke:

```python
state = graph.invoke(Command(resume={'action': 'poll'}), config)
```

Pending runs pause again. Native failures end in `FAILED`. Native success ends in
`EXECUTION_SUCCEEDED_REQUIRES_RECONCILIATION`; it does not authorize cutover. A
service may deliver the poll signal through an authenticated callback/queue in a
production deployment. The reference code does not install that event bridge.

For Azure, the graph derives the approved subscription/factory scope from the
bundle. For Databricks, bind an explicit `host` or `DATABRICKS_HOST` before graph
creation, so changing the workspace requires a new reviewed run.

`submit_approved.py` is an alternative direct SDK submission helper for integrating
with another control plane. It requires a SHA256 of the **whole bundle**, which is
different from the spec's `plan_hash`. Calculate it with
`datamigration.cloud.base.digest(bundle)`, review it, and pass
`--approve-bundle-sha256`, `--operation-key` and `--receipt` explicitly. It skips the
LangGraph UI/interrupt flow; the operator or invoking service owns authorization.

## Runtime prerequisites

- AWS: `pip install -e '.[aws]'`, normal AWS identity, approved state machine/job.
- Azure: `pip install -e '.[azure]'`, Azure identity, existing linked services and
  reviewed pipeline/datasets. Preserve the submission ledger for unknown outcomes.
- Databricks: `pip install -e '.[databricks]'`, workspace identity and reviewed job.
- Examples contain only placeholder resource IDs and environment variable names;
  there are no usable cloud credentials.

See [AWS](../../docs/architecture/aws.md), [Azure](../../docs/architecture/azure.md),
and [Databricks](../../docs/architecture/databricks.md) for the full boundaries and
deployment instructions.
