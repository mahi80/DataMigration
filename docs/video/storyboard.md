# Walkthrough storyboard

**Runtime:** 60 seconds exactly. **Canvas:** 16:9, default 1920 × 1080. **Style:**
teal and neutral product colors, original vector motion, large captions, subtle
dissolves, quiet original instrumental music. No fabricated metrics or customer
stories appear.

| Time | Purpose and visual | On-screen message | Implementation and scope |
|---|---|---|---|
| 00–06 | Introduce a shared workflow; reveal discovery, planning, approval and reconciliation stages | “A clear plan. A shared workspace.” | The [console](../../frontend/src/components/migration-console.tsx) exposes real persisted runs and role-aware actions. This is a title illustration. |
| 06–15 | Show registered connections and explicit column transformations | “Discover. Map. Make it explicit.” | The [wizard](../../frontend/src/components/migration-wizard.tsx) handles discovery, table selection, mappings, keys and quality rules. The illustrated fields and transformations come from the [synthetic demo](../../src/datamigration/demo.py). |
| 15–24 | Connect planner, approver and worker through one reviewed plan | “Approve the exact plan.” | [Run detail](../../frontend/src/components/run-detail.tsx) presents plan review and an explicit confirmation dialog. The workflow verifies the approved fingerprint. A changed plan requires a new review. |
| 24–33 | Animate the actual Ask Q character beside contextual guidance | “A little guidance. In context.” | Ask Q uses the current principal and selected run. Read-only monitoring is opt-in while the workspace remains open; suggestions use the existing role-checked workflow and confirmations. It does not silently approve, retry or correct a migration. |
| 33–42 | Show reconciliation evidence and a source-to-target column lineage example | “Reconcile. Explain. Trace.” | [Run detail](../../frontend/src/components/run-detail.tsx) exposes configured reconciliation scope, key differences, mapped-column checks, quality results, correction counts and lineage. The illustration does not invent a successful run result. |
| 42–51 | Connect the control plane to three documented execution paths | “Your cloud. A reviewed execution path.” | [AWS](../architecture/aws.md), [Azure](../architecture/azure.md) and [Databricks](../architecture/databricks.md) have distinct deployment/execution scopes. Native jobs use CLI/Python, and each environment needs qualification. EC2/VM pilots and future Fargate/Container Apps designs are labeled separately. |
| 51–60 | Close with the code repository and local-demo invitation | “Migration, with a plan.” | Link: [github.com/mahi80/DataMigration](https://github.com/mahi80/DataMigration). The [demo launcher](../../examples/console/serve_demo.py) creates synthetic data; no ROI, throughput or certification promise appears. |

## Motion and legibility

The introductory workflow reveals its stages. Mapping routes carry small animated
markers. Ask Q waves, blinks and floats using its real SVG geometry. Scenes use
brief dissolves rather than fabricated mouse clicks. Captions remain visible at
the bottom, with a fine elapsed-time line. The final card holds the repository
address long enough to read.

The current render uses clearly labeled workflow illustrations. The reusable
renderer can substitute authentic synthetic-demo screenshots without altering
them; see the [capture contract](README.md#visual-provenance-and-optional-screenshots).
