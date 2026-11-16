# DataMigration — 60-second walkthrough transcript

This caption-driven video has instrumental audio and no spoken narration. Visual
descriptions below make the complete content available as text.

## 00:00–00:06 — Coordinate the work

**Caption:** Migration is a team effort. Bring plans, approvals and evidence into
one workspace.

**Visual:** DataMigration branding appears above “A clear plan. A shared
workspace.” A four-stage workflow highlights Discover, Plan, Approve and
Reconcile. The supporting text describes coordinating the people, policies and
data behind a migration.

## 00:06–00:15 — Discover and map

**Caption:** Discover registered sources and targets. Choose tables, map columns
and define quality rules.

**Visual:** A labeled workflow illustration maps the synthetic `customers` source
to a target: `id` to `customer_id`, `name` to `full_name` using `trim`, and `email`
to `email` using `lower`. The selected key and source-stability prerequisite are
explicit. Side notes identify discovery, mapping and quality-rule configuration.

## 00:15–00:24 — Approve the exact plan

**Caption:** An authorized reviewer inspects the exact plan. Execution follows
approval of that plan fingerprint.

**Visual:** A plan contains the source, target, mappings, keys and quality rules.
The planner creates the plan, the approver reviews that version, and the worker
runs the approved scope. A changed plan needs a new review.

## 00:24–00:33 — Ask Q in context

**Caption:** Ask Q guides setup and follows the selected, authorized run. It
proposes next steps. You confirm permitted actions.

**Visual:** The actual Ask Q SVG character waves and blinks beside three forms of
help: guide setup, follow the selected run, and propose the next step. Read-only
updates are opt-in while the console is open. Proposed review or retry actions
use role checks and the existing confirmation flow.

## 00:33–00:42 — Inspect the evidence

**Caption:** Compare mapped data and inspect recorded quality outcomes. Keep
reconciliation scope, corrections and lineage visible.

**Visual:** A labeled evidence table lists mapped-data comparison, missing/extra/
duplicate-key reconciliation, and quality-rule outcomes with correction counts.
A lineage example traces `customers.name` through `trim` to
`customers.full_name`. It does not display an invented pass rate.

## 00:42–00:51 — Choose a deployment path

**Caption:** Follow reference architectures for AWS, Azure and Databricks. Native
cloud jobs use CLI or Python and need environment qualification.

**Visual:** The Next.js console and LangGraph control plane connect to three
documented paths. AWS uses Step Functions with Glue or reviewed DMS tasks. Azure
uses reviewed Data Factory pipelines. Databricks uses reviewed Jobs and Delta
Lake work, with Unity Catalog and optional Mosaic AI advice. Cloud container
pilots and future managed-service blueprints are labeled as such.

## 00:51–01:00 — Explore the code

**Caption:** Explore the framework and run the synthetic local demo.
`github.com/mahi80/DataMigration`

**Visual:** A dark teal closing card reads “Migration, with a plan.” Ask Q floats
beside the repository address. The supporting stack is Next.js, LangGraph and
Python.
