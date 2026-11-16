# Verification record

## Initial engine release (v0.1)

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

## Next.js console and Ask Q addition

The console and contextual assistant were checked on 2026-11-16 with Node.js
24.22.1 and Python 3.12.14. CI uses Node.js 22 and Python 3.12. The original engine
record above remains historical; the expanded local suite reports:

| Check | Observed result |
| --- | --- |
| Backend and deployment-probe tests | **269 passed**, including 23 console tests, 68 assistant tests and 16 startup-probe regressions |
| Backend coverage with branch tracking | **75% combined coverage** across the package |
| Frontend lint and generated-route TypeScript checks | Passed |
| Frontend unit tests | **16 passed** for session encryption, origin/proxy boundaries, backend response handling and draggable-panel geometry |
| Frontend helper coverage | **95.91% lines** for the configured server-helper scope; this is not total UI coverage |
| Next.js production build | Passed with the console and Ask Q integrated |
| Real Next.js → FastAPI HTTP integration | **3 passed**: reviewed migration, session/proxy boundaries, and read-only Ask Q guidance |
| Video export | Exactly **60.000 seconds**, 1920 × 1080, 30 fps, 1,800 frames, stereo AAC |

The assistant tests exercise role and table/connection scopes, malicious client
events, plan integrity, no migration writes or checkpoint hydration, model data
minimization, invalid provider output and timeout fallback. External model
services were not called. The optional model selects existing grounded help
sections; these results do not imply unrestricted autonomous reasoning or repair.

Local Chromium could not start because the authoring workspace prohibits its
UNIX process sockets. The requested execution escalation was automatically
rejected. The three local HTTP integration cases use real Next.js/FastAPI
processes and require no browser; they are distinct from visual verification.

The [first console GitHub browser run](https://github.com/mahi80/DataMigration/actions/runs/43831146933)
passed six of seven scenarios, including actual planner → approver → reconciliation,
viewer restrictions, rejection, CSRF and HTTP integration. Its mobile overflow
assertion failed. The table scroll container now establishes a positioned
containing block for the accessible header label; the expanded browser suite
also checks Ask Q drag/keyboard movement, context, minimized monitoring and
reduced motion. The [Ask Q GitHub run](https://github.com/mahi80/DataMigration/actions/runs/43832319555)
passed **all 11 browser and HTTP scenarios**, including the corrected mobile
layout. Its API container image built successfully, but the startup probe failed
on a connection reset before the server was ready. The API and frontend probes
now share a bounded readiness helper that retries transient transport errors
while preserving exact HTTP/status checks and the deadline. Sixteen regression
cases cover that change. The final CI results below include actual container
startup checks, not only image builds.

### Final console CI result

The [final application verification run](https://github.com/mahi80/DataMigration/actions/runs/43834230987)
checked commit [`19bd99d`](https://github.com/mahi80/DataMigration/commit/19bd99dc73a0ff843b015c6a488a9ea3e5920e17)
with the following observed results:

| CI check | Observed result |
| --- | --- |
| Backend and deployment-probe tests | **269 passed**, 75% combined branch-aware package coverage |
| Python lint, syntax, wheel and source distribution | Passed |
| Frontend lint, TypeScript and production build | Passed |
| Frontend unit tests | **16 passed** |
| Chromium and real HTTP integration | **11 passed**: eight browser scenarios and three HTTP scenarios |
| API container | Built and started; health, unauthenticated-call rejection and shared persistent maintenance barrier passed |
| Frontend container | Built and started; HTTP process/configuration health passed |
| SonarQube configuration | **Blocked**: `Configure SONAR_HOST_URL as the approved HTTPS SonarQube server URL` |
| SonarQube scan and quality gate | **Not run** because configuration is missing |

The expanded browser checks verify that a dragged Ask Q panel stays separate
from its launcher and that an external approval detected by the watcher refreshes
the underlying migration view and removes stale approval controls. Actual
Chromium screenshots of the desktop workflow, mobile layout and Ask Q panels
were inspected. The local Docker limitation is therefore separate from these
successful GitHub container checks; no AWS/Azure deployment was performed.

The workflow's overall conclusion remains **failure** at the required SonarQube
configuration step. Configure repository variable `SONAR_HOST_URL` and secret
`SONAR_TOKEN` using [CI/CD](cicd.md), then rerun and inspect the actual analysis
and quality-gate result. Application tests do not establish a passed SonarQube
gate. This verification-record update changes documentation only; the linked
run identifies the tested application commit explicitly.

The video is a caption-led motion-graphic walkthrough using labeled workflow
illustrations and the actual SVG mascot. Its source, subtitles and transcript
are under [video/](video/README.md); it is not a recording of a production
migration or evidence of a live cloud rollout.
