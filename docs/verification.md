# v0.1 verification record

Verified on 2026-11-16 in Linux with Python 3.12.14 and uv 0.13.6. Dependencies
were installed from the committed `uv.lock` using the same extras as CI.

## Results

| Check | Observed result |
| --- | --- |
| Ruff | Passed |
| Automated tests | **162 passed**, no skipped tests in the final run |
| Coverage with branch tracking | **74% combined coverage** across the package; not a SonarQube gate result |
| Wheel and source distribution | Built successfully |
| SQLite OLTP CLI demo | Completed and reconciled all four tables |
| SQLite star schema → local Delta CLI demo | Completed and reconciled both dimensions and the fact table |
| CSV → Parquet executable example | Completed with exact typed-value reconciliation |
| Reviewed nested CTE → SQLite executable example | Completed with a bound query parameter and reconciliation |
| SQLAlchemy ORM source example | Completed and reconciled the reflected physical table |
| Source inventory / typed query discovery | CLI succeeded; table/view inventory exported |
| AWS, Azure and Databricks example compilation | Produced reviewable bundles without cloud submission |
| Unreviewed DMS example | Rejected before bundle submission as intended |
| Ontop export | Produced mapping, ontology, properties template and manifest |
| MCP | Official SDK stdio handshake and tool calls passed |
| A2A | Agent Card and task messages validated against official SDK models; approval lifecycle passed |
| Workflow and script checks | Actionlint, YAML/JSON parsing, Bash/Python syntax and Docker RUN shell syntax passed |
| Documentation | Local relative links resolved |

The test suite includes real local storage operations and injected failures, not
only mocked happy paths. Examples include a destination write committed before
its acknowledgement is lost, retrying a native submission with the same operation
identity, detecting schema/endpoint/CSV-codec drift after approval, preventing
out-of-scope access, and rejecting duplicate final keys before destination writes.

The CLI executable examples were run separately from pytest, so their execution
is not counted in the coverage report. Coverage highlights remaining areas for
qualification; it is not evidence that every engine, branch or production failure
mode has been tested.

## What was not run

No live PostgreSQL/MySQL/SQL Server/MongoDB/Redis/Cassandra/Redshift/Databricks
service migration, AWS/Azure deployment, external model call, OpenMetadata server
publication, or Ontop JDBC endpoint was exercised. Contract tests and actual
local adapters are identified separately in the connector guide.

Docker is unavailable in the authoring environment, so the container was not
built or started there. The production dependency selection was checked against
the frozen lock, and the provided CI workflow builds and smoke-tests the image.
No SonarQube server was configured or scanned. Configure it and inspect its actual
quality-gate result before deployment; the workflows fail when the required gate
is missing, red or unavailable.

### Initial GitHub CI attempt

After publication, [the initial CI run](https://github.com/mahi80/DataMigration/actions/runs/43826581510)
checked out the code and set up Python, then failed **Verify SonarQube
configuration** with `Configure SONAR_HOST_URL as the approved HTTPS SonarQube
server URL`. Test, image and analysis steps in that run were skipped after the
preflight failure. The local results above remain separate evidence; they are
not presented as a successful GitHub CI or SonarQube scan. Set the repository
variable `SONAR_HOST_URL` and the project-analysis secret `SONAR_TOKEN`, then rerun
CI. The complete setup is in [CI/CD](cicd.md).

No throughput, monthly-cost, high-availability or customer-scale certification
is claimed. Use the deployment and platform-selection guides to qualify those
properties for the actual source/target pair and workload.

## Reproduce

```bash
python -m pip install 'uv==0.13.6'
uv sync --frozen --extra dev --extra aws --extra azure --extra databricks
uv run --no-sync ruff check .
uv run --no-sync pytest --cov=datamigration --cov-config=infra/coverage.ini --cov-report=term
uv run --no-sync python -m build
```

The README supplies the runnable demo and example commands. Use fresh output
directories for synthetic examples; they intentionally refuse to overwrite data.
