# KQL report pack

Six read-only reports over the existing transcript, agent, and user datasets.
No new tables, stored functions, ingestion, or dashboard import is required.

## Run a report

1. Open [Azure Data Explorer](https://dataexplorer.azure.com), connect to your
   cluster, and select `CopilotTranscripts` (or your configured `ADX_DATABASE`).
2. Open a `.kql` file below and paste its **entire contents** into a query tab.
3. Adjust `ReportStart`, `ReportEnd`, and `IncludeTestPane` at the top, then
   select and run the whole file, including the `let` statements.

Start with **06_data_quality.kql** to see how much of the archive the other
reports can describe. Each query returns one result set; use the results grid
to export CSV, or pin it as a dashboard tile. Daily usage also renders a chart.

| File | Question it answers |
|---|---|
| [01_daily_usage.kql](01_daily_usage.kql) | Is observed usage growing, and how many users and agents are active each day? |
| [02_department_adoption.kql](02_department_adoption.kql) | Which departments use agents, how often, and with what outcomes? |
| [03_agent_performance.kql](03_agent_performance.kql) | Which agents carry the workload, resolve engaged sessions, escalate, or lose users? |
| [04_repeat_usage.kql](04_repeat_usage.kql) | Do identified users come back on another day, or only use an agent on one day? |
| [05_unused_published_agents.kql](05_unused_published_agents.kql) | Which agents have a publication timestamp but no observed sessions in the window? |
| [06_data_quality.kql](06_data_quality.kql) | How much traffic is test-pane, unattributed, unmatched to inventory, or unparseable? |

For a fixed interval, replace the first two declarations, for example:

```kusto
let ReportStart = datetime(2026-08-01);
let ReportEnd = datetime(2026-09-01);
```

The window is **UTC, start-inclusive and end-exclusive**. Both partial boundary
days are included in daily and repeat usage; choose midnight boundaries for
complete-day comparisons. Reports default to the past 30 days. Days without
session records are absent, not asserted to have zero activity. Daily distinct
users and agents are not additive across days.

`IncludeTestPane = false` excludes explicitly flagged Copilot Studio test-pane
traffic. Following the existing semantic layer, missing design-mode flags are
included, **not proof of production traffic**; the quality report counts them
separately. A tenant with only test-pane traffic can legitimately return empty
usage reports. Set the flag to `true` deliberately when investigating it.

## Data sources and joins

Select **`CopilotTranscripts`** as the query database. The standard deployment
syncs to three landing tables in that database:

| Synced table | How reports read it |
|---|---|
| `CopilotTranscriptRaw` | `CopilotTranscript` materialized view, then `CopilotTranscriptTurn()` and `CopilotSession()` |
| `CopilotAgentRaw` | Query-local latest snapshot per `(EnvironmentId, BotId)` |
| `CopilotUserRaw` | `CopilotUser()` returns the latest snapshot per `AadObjectId` |

The transcript table is **`CopilotTranscriptRaw`** (singular `Transcript`),
not `CopilotTranscriptsRaw`. The database name uses the plural
`CopilotTranscripts`. The sync's `ADX_RAW_TABLE` setting and the materialized
view's source must point to the same landing table.

`CopilotSession()` and `CopilotUser()` are stored functions in this same
database, not separate tables or databases. The data-quality report also reads
`CopilotTranscript` directly to include parse failures that cannot become
sessions. Reading through these objects avoids counting overlapping sync
copies as new activity.

Apply `kql\01_tables.kql` through `kql\05_users.kql` to install these dependencies
if they are not already deployed. The report files live outside `kql\` so
`scripts\deploy_kql.py` will not mistake them for schema-management commands.

Agent snapshots are deduplicated **before** joining; repeated syncs must not
multiply session counts. These queries read the raw dimension intentionally:
the existing `CopilotAgent()` groups only by `BotId`, whereas these reports
preserve the same bot ID in different environments. The join is
`(session.EnvironmentId, session.DataverseBotId)` to
`(agent.EnvironmentId, agent.BotId)`. The transcript's runtime `BotId` is **not**
the Dataverse foreign key.

Usage reports retain sessions without matching metadata. The performance
report falls back to the transcript schema name, then an ID; missing
Dataverse IDs use runtime IDs only for grouping, never inventory joins. If
both agent IDs are missing, those sessions form one unknown-agent group per
environment. Agent names alone are never grouping keys.

If your deployment actually splits the datasets across databases, qualify the
source references with `database("YourDatabase").Object` (for functions, retain
`()`). The session function must exist in the transcript database. Cross-database
access requires permission on each database; nothing here changes permissions.

## How to interpret the numbers

**Session grain.** These reports count `SessionInfo` records exposed by
`CopilotSession()`, not transcript rows, messages, or billable credits. A
conversation can contain multiple sessions and span transcript batches.
Transcripts without a valid session record are absent from session metrics.

**Outcome denominators.** Engagement is engaged sessions divided by all report
sessions. Resolution, escalation, and abandonment percentages use **engaged
sessions only**, in both numerator and denominator. No engaged sessions means
a null percentage, not 0% or infinity. Outcome rates need not sum to 100%:
other or missing outcomes can exist. These are recorded outcomes, not
independently verified answer quality, customer satisfaction, or business value.
`SmallOutcomeSample` flags fewer than `MinimumEngagedSample` engaged sessions
(default 20); it is a caution, not a statistical significance test.

**People and departments.** `KnownUsers` counts nonempty Entra object IDs, not
every human who used an agent. Most distinct counts use Kusto `dcount` and can
be approximate. Department adoption preserves separate `Resolved`,
`Unresolved`, and `No Entra ID` buckets. A resolved user with no department is
also labeled separately. A missing Entra ID is not proof that a user was
unauthenticated. `SessionsPerKnownUser` is null when no IDs are present.

The user dimension contains **only users observed in transcripts**, not an
employee roster. These data cannot measure percentage adoption across a
department's workforce or identify employees who have never used an agent.
User sync must be enabled (`SYNC_USERS=true`) and authorized for departmental
segmentation; otherwise sessions remain visible in unattributed buckets.

**Repeat usage.** A returning user is active on at least two distinct UTC days
within the selected window. Multiple sessions on one day do not qualify.
The denominator is identified users in the same department/attribution bucket.
Anonymous sessions are excluded; unresolved but identified users are retained.
This is not cohort retention, lifetime repeat usage, or evidence of productivity.
Two sessions across midnight can qualify even if only minutes apart.

**No observed usage.** The inventory report requires a nonnull `PublishedOn`
at least `MinimumPublishedAge` before the window end (default 7 days).
Unpublished rows and newer publications are excluded. A publication timestamp
does not prove an agent is currently available: inspect `StateLabel` and
`MetadataLastSeen`. Zero sessions can mean no traffic, disabled transcript
saving, missing foreign keys, collection failure, or an agent deleted since its
last snapshot. It is a review list, not an automatic retirement recommendation.

**Current dimensions.** User departments, agent names, publication fields, and
inventory are the latest retained snapshots, not values as of each session.
Historical windows are therefore segmented by current metadata. Inventory can
be stale when sync is disabled or an object is deleted.

**Coverage.** The quality report includes environments present in inventory
even if they have no sessions or transcripts. It cannot discover environments
absent from all three sources. `AllSessions`, `TestPaneSessions`, and
`UnknownDesignModeSessions` describe all session traffic in the window; identity
and agent-match counts/percentages describe only `ReportSessions`, respecting
`IncludeTestPane`. Archive counts and parse percentages instead use transcript
`CreatedOn` in the same interval, include test traffic, and are not expected to
reconcile one-to-one with sessions. Unparseable content can never contribute
session facts. Ingestion timestamps indicate when rows were read by the sync,
not a pipeline heartbeat or a freshness SLA.

**Privacy.** Reports aggregate usage and avoid prompt text, response text,
user names, and email addresses. The inventory report includes owner IDs for
administrative follow-up. Departmental groups may still be small enough to
identify individuals; apply your organization's access and export controls.
No query grants access or suppresses small groups automatically.

## Development checks

The existing pytest runner checks the report catalog and read-only query
contracts without Azure. Opt in to Kusto execution with an Azure CLI login
and database Viewer access:

```powershell
$env:KQL_REPORT_TEST_CLUSTER = "https://yourcluster.eastus.kusto.windows.net"
$env:KQL_REPORT_TEST_DATABASE = "CopilotTranscripts"
.\.venv\Scripts\python.exe -m pytest tests\test_reports.py tests\test_kql_deploy.py -q
```

The opt-in cases execute each report against the deployed schema and exercise
query-local synthetic and empty datasets. They cover time boundaries,
test-pane inclusion, environment-scoped keys, duplicate agent snapshots,
unmatched metadata, repeat-day counting, and zero denominators. Requests are
read-only; the synthetic data is never ingested.
