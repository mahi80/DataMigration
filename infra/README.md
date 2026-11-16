# Reviewed deployment artifacts

The [host deployment scripts](deploy/README.md) and [CI/CD guide](../docs/cicd.md)
provide a single-node EC2/Azure VM rollout with SonarQube gates, OIDC, digest-pinned
images and maintenance-aware replacement. They use local encrypted block storage
for the shipped SQLite state. ECS/Container Apps distributed topologies remain
future architecture work; do not put this workspace on EFS or Azure Files.

The JSON files here are examples compiled from `examples/cloud/*.json`. Regenerate
artifacts from your actual reviewed spec; do not deploy placeholder IDs.

- `aws/state_machine.example.json`: Step Functions Standard definition using an
  existing approved Glue job. Create the scoped service role and worker separately.
- `azure/arm_template.example.json`: ARM deployment into an existing factory;
  creates datasets and the pipeline only. Existing linked services own connectivity
  and credentials. Standalone pipeline/dataset JSON is included for inspection.
- `databricks/job.example.json`: job creation template for the provided Spark
  Delta-to-Delta example; substitute the reviewed cluster and workspace script path.
- `databricks/delta_table_job.py`: explicit pinned-snapshot Spark job. Requires
  Databricks Runtime/Spark/Delta; it has not been run against a live workspace.

Cloud deployment and source/target provisioning are deliberate operator actions.
The repository does not create broad IAM roles, publicly expose databases, create
billable clusters, install enterprise governance servers, or run migrations merely
because the Python package is installed.

The production control-plane diagrams include shared state, authentication,
networking and event delivery. The shipped local CLI/API/checkpoint implementation
is a single-host workbench; provision and qualify those distributed controls before
using the diagrams as an enterprise deployment design.
