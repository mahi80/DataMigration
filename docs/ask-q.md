# Ask Q: contextual migration guidance

Ask Q is the draggable SVG companion in the signed-in Next.js console. It helps
you configure a migration, understand recorded run evidence and open a permitted
next step. **Guided mode works locally without a model service.** Its LangGraph
workflow selects curated help cards grounded in this repository's documentation
and reads fresh, authorized server artifacts for the selected run.

An optional model can select and order **existing help-card IDs**. It cannot
generate answer prose, SQL, code, permissions or actions. Ask Q has no migration
execution tools; suggested actions open the existing workflow screens.

## Use Ask Q

Start the [console demo](../README.md#nextjs-console) or sign in to your configured
workspace, then select the **Ask Q** character.

1. **Set up:** choose **Guide my setup**. Review connection registration, source
   stability, keys, mappings and quality rules. **Prepare a plan** opens the
   migration wizard when your role permits planning.
2. **Check status:** open a migration, then choose **Explain this run**. Q reads
   the saved status and available validation summary within your connection and
   table scope. A recorded `RUNNING` state does not prove worker liveness or give
   a completion-time estimate.
3. **Investigate a failure:** choose **Help fix an issue**. Q classifies bounded
   saved error evidence into categories such as connectivity, schema drift,
   duplicate keys or quality checks, and suggests an investigation. It does not
   infer the correct business value or repair records. Inspect worker evidence
   and partial writes before recovery; changed mappings need a new reviewed plan.
4. **Review:** an authorized approver can follow **Review the exact plan** to the
   mapping/review section. The existing review checkbox, displayed fingerprint,
   **Approve and run** button and confirmation dialog still apply.
5. **Recover:** when the saved state and role allow it, **Review recovery** opens
   the run's recovery section. It does not submit a retry. The operator must
   investigate, select the existing retry control and confirm; the API rechecks
   the role, state and retained approval.

Suggestions respect the authenticated subject, role, connection/options and table
scopes. Planners/operators can prepare plans; approvers/admins can approve;
operators/admins can request permitted recovery. Viewers receive scoped status
guidance. Maintenance mode withholds new-plan and retry suggestions. See
[roles and scopes](protocols-and-rbac.md) for the full authorization contract.

## Questions it supports

| Ask about | Example |
|---|---|
| Setup and prerequisites | “How do I set up a migration?” |
| Current run and failure evidence | “Why did this run fail, and what should I review?” |
| Approval and access | “Who can approve this plan?” |
| Connectors and data models | “Which databases and flat files are supported?” or “What does star-schema support mean?” |
| Quality and lineage | “Which corrections are supported?” or “How does column lineage work?” |
| SQL and interoperability | “Can it migrate stored procedures?” or “How do MCP and A2A work?” |
| Cloud and governance | “How do AWS, ADF or Databricks jobs fit?” or “Where do OpenMetadata and Ontop fit?” |
| Deployment and selection | “What production work remains?” or “How should I compare platform cost?” |

Answers link to the relevant repository guides. This is curated product guidance,
not a live web/repository search or a pricing feed. It does not examine arbitrary
application code, infer business joins or translate stored procedures.

## Monitor a selected run

Open a migration and select **Start** in **Monitor this run**. Q performs
read-only status checks every **8 seconds**. Monitoring continues when the Q
panel is minimized; a watch indicator remains on the character, and a status
change adds a badge and conversation notice. A detected change also refreshes
the selected migration and workspace summary from the authorized API, so a run
approved in another session no longer leaves stale approval controls on screen.

Checks pause while the browser tab is hidden. They stop when you select **Stop**,
change the selected run or signed-in identity, sign out/close the workspace, reach
`COMPLETED` or `REJECTED`, or encounter an API error. Monitoring calls the normal
status endpoint, never a model, approval, retry or write endpoint. It provides
in-tab observations, not an unattended background monitoring service. Closing the
browser does not cancel a migration already running on the API host.

## Controls and accessibility

| Control | Behavior |
|---|---|
| Mouse or touch drag | Move the character; position stays within the viewport |
| Arrow keys on the character | Move 16 pixels; **Shift + arrow** moves 40 pixels |
| **Home** on the character | Reset to the lower-right corner |
| **Enter** or **Space** on the character | Open/minimize the panel |
| **Escape** in the panel | Close the panel and return focus to the character |
| **Enter** in the composer | Send the question; **Shift + Enter** adds a line |
| Clear-conversation button | Clear the in-memory messages and recent-action buffer |

The character and panel respect the browser's reduced-motion preference. Only
the character's `{x, y}` position is saved to browser local storage.

## Context and privacy

The browser sends the current question (up to 2,000 characters), current view,
selected run ID and up to eight allowlisted recent action names/outcomes to the
authenticated API. It does not scrape page text or send form values, raw rows,
connection credentials or the full chat history. The API derives identity from
authentication and rereads bounded authorized run/plan artifacts; browser events
cannot establish permissions, successful execution or approval.

Messages and the recent-action buffer stay in the current tab's memory. The
assistant backend has no transcript store or checkpoint, and disables LangSmith
tracing for this graph. Do not paste credentials into a question: the current
question is still sent to your API for local classification.

If model assistance is enabled, the external request contains only topic IDs,
view, role, status, source/target connector kinds, table count, a bounded issue
category, candidate card IDs/titles and mandatory card IDs. It excludes the user
question, conversation, subject, run/table names, environment references, URLs,
queries, raw errors and row values. Selected IDs are validated against the
allowlist; mandatory status/issue evidence is preserved. Timeout, unavailable
providers or malformed output fall back to **Guided mode**.

## Optional model configuration

Set `MIGRATION_ASSISTANT_CONFIG` on the **Python API server**, before starting it.
It is independent of a migration specification's metadata advisor. Leave it
unset, or use the following, for the complete local guided experience:

```bash
export MIGRATION_ASSISTANT_CONFIG='{"provider":"disabled"}'
```

To enable a compatible, administrator-approved model endpoint, supply its endpoint
and credential through the API host's secret/environment system, then reference
their variable names:

```bash
export MIGRATION_ASSISTANT_CONFIG='{
  "provider": "openai_compatible",
  "model": "approved-model-or-deployment",
  "base_url_env": "ASK_Q_BASE_URL",
  "api_key_env": "ASK_Q_API_KEY",
  "max_tokens": 512,
  "timeout_seconds": 5.0
}'
```

| Field | Contract |
|---|---|
| `provider` | `disabled` (default), `openai_compatible`, `azure`, `databricks`, or `bedrock` |
| `model` | Required for an enabled provider; model/deployment identifier |
| `base_url_env` | Endpoint environment-variable name; default `MIGRATION_LLM_BASE_URL` |
| `api_key_env` | Credential environment-variable name; default `MIGRATION_LLM_API_KEY` |
| `max_tokens` | Default 512; allowed 128–2,048 |
| `timeout_seconds` | Default 5; allowed 0.1–15 seconds |
| `region` | Optional AWS region for Bedrock; otherwise unused |

For HTTP providers, the adapter appends `/chat/completions` to the configured base
and expects the implemented chat-completions request/response contract. Azure uses
an `api-key` header; the other HTTP providers use a bearer credential. Configure a
compatible endpoint or gateway: a provider label does not translate every vendor
API version. Remote endpoints require HTTPS; loopback HTTP is accepted for local
development. Embedded credentials, URL queries/fragments and redirects are not
accepted. Bedrock uses boto3 `converse` and the API host's AWS credential chain;
install the `aws` extra and grant the required model access.

Never place this configuration or model credentials in `NEXT_PUBLIC_*`, the chat
request or the frontend's browser state. Invalid configuration is rejected at API
startup. Provider calls are bounded and fall back locally when unavailable; live
endpoint qualification remains an operator task.

Implementation: [read-only assistant graph](../src/datamigration/assistant.py),
[console component](../frontend/src/components/ask-q.tsx), and
[SVG character](../frontend/src/components/ask-q-mascot.tsx). The
[60-second walkthrough sources](video/README.md) include reusable rendering code,
captions, storyboard and transcript; the rendered MP4 is delivered separately.
