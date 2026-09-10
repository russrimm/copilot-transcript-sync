"""Report contracts plus opt-in, read-only Kusto execution tests.

Set KQL_REPORT_TEST_CLUSTER and optionally KQL_REPORT_TEST_DATABASE to run the
Kusto tests with AzureCliCredential. Synthetic inputs are query-local datatables;
no database objects or rows are created, changed, or deleted.
"""

from __future__ import annotations

import os
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
REPORT_DIR = ROOT / "reports"
REPORT_NAMES = [
    "01_daily_usage.kql",
    "02_department_adoption.kql",
    "03_agent_performance.kql",
    "04_repeat_usage.kql",
    "05_unused_published_agents.kql",
    "06_data_quality.kql",
]

FIXTURES = """
let EmptyFixtures = false;
let CopilotSession = () {
    datatable(
        EnvironmentId:string, EnvironmentName:string, DataverseBotId:string,
        BotId:string, BotName:string, AadObjectId:string, SessionStart:datetime,
        IsDesignMode:bool, IsEngaged:bool, IsResolved:bool, IsEscalated:bool,
        IsAbandoned:bool, TurnCount:int, SessionDuration:timespan
    ) [
        "e1", "One", "a", "runtime-a", "schema-a", "u1", datetime(2026-08-01), false, true, true, false, false, 2, 10s,
        "e1", "One", "a", "runtime-a", "schema-a", "u1", datetime(2026-08-01 09:00), false, true, false, true, false, 4, 20s,
        "e1", "One", "a", "runtime-a", "schema-a", "u1", datetime(2026-08-02), false, true, false, false, true, 6, -1s,
        "e1", "One", "b", "runtime-b", "schema-b", "u2", datetime(2026-08-02), false, false, true, false, false, -1, timespan(null),
        "e2", "Two", "a", "runtime-c", "schema-c", "u3", datetime(2026-08-02), false, true, true, false, false, 2, 30s,
        "e1", "One", "", "runtime-missing", "schema-missing", "", datetime(2026-08-03), false, false, false, false, false, 1, timespan(null),
        "e1", "One", "ghost", "runtime-ghost", "schema-ghost", "u4", datetime(2026-08-03), false, true, false, false, false, 1, 5s,
        "e1", "One", "test-only", "runtime-test", "schema-test", "u3", datetime(2026-08-04), true, true, true, false, false, 2, 10s,
        "e1", "One", "a", "runtime-a", "schema-a", "u5", datetime(2026-08-04), bool(null), false, false, false, false, 0, 0s,
        "e1", "One", "a", "runtime-a", "schema-a", "u1", datetime(2026-09-01), false, true, true, false, false, 2, 10s,
        "e1", "One", "a", "runtime-a", "schema-a", "u1", datetime(2026-07-31 23:59:59), false, true, true, false, false, 2, 10s
    ]
    | where not(EmptyFixtures)
};
let CopilotUser = () {
    datatable(AadObjectId:string, Resolved:bool, Department:string) [
        "u1", true, "Finance",
        "u2", true, "",
        "u3", true, "Finance",
        "u4", false, "Do not attribute",
        "", true, "Do not match anonymous"
    ]
    | where not(EmptyFixtures)
};
let CopilotAgentRaw =
    datatable(
        EnvironmentId:string, EnvironmentName:string, BotId:string, Name:string,
        SchemaName:string, PublishedOn:datetime, StateLabel:string, OwnerId:string,
        IngestedAt:datetime
    ) [
        "e1", "One", "a", "Old name", "schema-a", datetime(2026-07-01), "Active", "owner", datetime(2026-07-01),
        "e1", "One", "a", "Alpha", "schema-a", datetime(2026-07-01), "Active", "owner", datetime(2026-08-30),
        "e2", "Two", "a", "Alpha", "schema-c", datetime(2026-07-01), "Active", "owner", datetime(2026-08-31),
        "e3", "Inventory only", "a", "No traffic", "schema-a", datetime(2026-07-01), "Active", "owner", datetime(2026-08-30),
        "e1", "One", "b", "Beta", "schema-b", datetime(2026-07-01), "Active", "owner", datetime(2026-08-30),
        "e1", "One", "dormant", "Dormant", "schema-d", datetime(2026-07-01), "Active", "owner", datetime(2026-07-01),
        "e1", "One", "test-only", "Test only", "schema-test", datetime(2026-07-01), "Active", "owner", datetime(2026-08-30),
        "e1", "One", "unpublished", "Unpublished", "schema-u", datetime(2026-07-01), "Active", "owner", datetime(2026-07-01),
        "e1", "One", "unpublished", "Unpublished", "schema-u", datetime(null), "Active", "owner", datetime(2026-08-30),
        "e1", "One", "fresh", "New publication", "schema-f", datetime(2026-08-30), "Active", "owner", datetime(2026-08-30),
        "e1", "One", "future", "Future publication", "schema-future", datetime(2026-09-02), "Active", "owner", datetime(2026-08-30),
        "e1", "One", "", "Invalid key", "", datetime(2026-07-01), "Active", "owner", datetime(2026-08-30)
    ]
    | where not(EmptyFixtures);
let CopilotTranscript =
    datatable(EnvironmentId:string, EnvironmentName:string, CreatedOn:datetime,
              ContentParseError:string, IngestedAt:datetime) [
        "e1", "One", datetime(2026-08-01), "", datetime(2026-08-02),
        "e1", "One", datetime(2026-08-03), "Invalid JSON", datetime(2026-08-04),
        "e2", "Two", datetime(2026-08-02), "", datetime(2026-08-03),
        "e4", "Transcript only", datetime(2026-08-02), "Invalid JSON", datetime(2026-08-03),
        "e1", "One", datetime(2026-09-01), "Outside window", datetime(2026-09-02)
    ]
    | where not(EmptyFixtures);
"""


def report_query(name: str, *, synthetic: bool = False, empty: bool = False,
                 include_test_pane: bool = False) -> str:
    query = (REPORT_DIR / name).read_text(encoding="utf-8")
    query = query.replace(
        "let IncludeTestPane = false;",
        f"let IncludeTestPane = {str(include_test_pane).lower()};",
    )
    if synthetic:
        query = query.replace("let ReportStart = ago(30d);", "let ReportStart = datetime(2026-08-01);")
        query = query.replace("let ReportEnd = now();", "let ReportEnd = datetime(2026-09-01);")
        prelude = FIXTURES.replace(
            "let EmptyFixtures = false;", f"let EmptyFixtures = {str(empty).lower()};"
        )
        query = prelude + query
    return query


def test_report_catalog():
    assert sorted(p.name for p in REPORT_DIR.glob("*.kql")) == REPORT_NAMES
    guide = (REPORT_DIR / "README.md").read_text(encoding="utf-8")
    for name in REPORT_NAMES:
        assert f"]({name})" in guide
        assert not (ROOT / "kql" / name).exists()


@pytest.mark.parametrize("name", REPORT_NAMES)
def test_report_read_only_contract(name):
    query = report_query(name)
    assert query.count("let ReportStart = ago(30d);") == 1
    assert query.count("let ReportEnd = now();") == 1
    assert query.count("let IncludeTestPane = false;") == 1
    assert "SessionStart >= ReportStart and SessionStart < ReportEnd" in query
    assert "IncludeTestPane or not(coalesce(IsDesignMode, false))" in query
    assert not any(line.lstrip().startswith(".") for line in query.splitlines())
    assert "kusto.windows.net" not in query


@pytest.fixture(scope="module")
def execute_kql():
    cluster = os.environ.get("KQL_REPORT_TEST_CLUSTER")
    if not cluster:
        pytest.skip("Set KQL_REPORT_TEST_CLUSTER for read-only Kusto report tests")
    from azure.identity import AzureCliCredential
    from azure.kusto.data import ClientRequestProperties, KustoClient, KustoConnectionStringBuilder

    database = os.environ.get("KQL_REPORT_TEST_DATABASE", "CopilotTranscripts")
    properties = ClientRequestProperties()
    properties.set_option("request_readonly", True)
    credential = AzureCliCredential(process_timeout=120)
    with KustoClient(KustoConnectionStringBuilder.with_azure_token_credential(cluster, credential)) as client:
        def execute(query):
            result = client.execute_query(database, query, properties)
            assert len(result.primary_results) == 1
            return [row.to_dict() for row in result.primary_results[0]]

        yield execute


@pytest.mark.parametrize("name", REPORT_NAMES)
@pytest.mark.parametrize("include_test_pane", [False, True])
def test_live_report_executes(execute_kql, name, include_test_pane):
    execute_kql(report_query(name, include_test_pane=include_test_pane))


@pytest.mark.parametrize("name", REPORT_NAMES)
def test_empty_sources(execute_kql, name):
    assert execute_kql(report_query(name, synthetic=True, empty=True)) == []


@pytest.mark.parametrize("include_test_pane", [False, True])
def test_daily_usage_boundaries(execute_kql, include_test_pane):
    rows = execute_kql(report_query(REPORT_NAMES[0], synthetic=True, include_test_pane=include_test_pane))
    assert [row["Day"].day for row in rows] == [1, 2, 3, 4]
    assert sum(row["Sessions"] for row in rows) == (9 if include_test_pane else 8)
    assert sum(row["ResolvedSessions"] for row in rows) == (3 if include_test_pane else 2)
    assert rows[0]["Sessions"] == 2
    assert rows[1]["AgentsUsed"] == 3  # a in two environments, plus b
    assert rows[2]["KnownUsers"] == 1  # anonymous traffic is not a person


def test_department_attribution_and_denominators(execute_kql):
    rows = execute_kql(report_query(REPORT_NAMES[1], synthetic=True))
    assert sum(row["Sessions"] for row in rows) == 8
    departments = {row["Department"]: row for row in rows if row["Attribution"] == "Resolved"}
    finance = departments["Finance"]
    assert finance["Sessions"] == 4
    assert finance["KnownUsers"] == 2
    assert finance["AgentsUsed"] == 2
    assert finance["ResolutionPct"] == 50.0
    missing = departments["(department missing)"]
    assert missing["EngagedSessions"] == 0
    assert missing["ResolvedSessions"] == 0
    assert missing["ResolutionPct"] is None
    unattributed = {row["Attribution"]: row for row in rows if row["Attribution"] != "Resolved"}
    assert unattributed["Unresolved"]["Sessions"] == 2
    assert unattributed["Unresolved"]["Department"] == "(not attributable)"
    assert unattributed["No Entra ID"]["KnownUsers"] == 0
    assert unattributed["No Entra ID"]["SessionsPerKnownUser"] is None


def test_agent_keys_snapshot_deduplication_and_duration(execute_kql):
    rows = execute_kql(report_query(REPORT_NAMES[2], synthetic=True))
    assert len(rows) == 5
    assert sum(row["Sessions"] for row in rows) == 8
    agents = {(row["EnvironmentId"], row["AgentKey"]): row for row in rows}
    alpha = agents["e1", "dataverse:a"]
    assert alpha["Agent"] == "Alpha"
    assert alpha["Sessions"] == 4
    assert alpha["InventoryMatched"] is True
    assert alpha["DurationSamples"] == 3  # invalid negative duration is omitted
    assert alpha["MedianDurationSeconds"] == 10.0
    assert alpha["ResolutionPct"] == 33.3
    assert alpha["SmallOutcomeSample"] is True
    assert agents["e2", "dataverse:a"]["Sessions"] == 1
    beta = agents["e1", "dataverse:b"]
    assert beta["ResolutionPct"] is None
    assert beta["AvgTurns"] is None
    assert beta["MedianDurationSeconds"] is None
    assert agents["e1", "dataverse:ghost"]["InventoryMatched"] is False
    assert agents["e1", "runtime:runtime-missing"]["Agent"] == "schema-missing"


@pytest.mark.parametrize("include_test_pane", [False, True])
def test_repeat_usage_requires_distinct_days(execute_kql, include_test_pane):
    rows = execute_kql(report_query(REPORT_NAMES[3], synthetic=True, include_test_pane=include_test_pane))
    assert sum(row["KnownUsers"] for row in rows) == 5
    finance = next(row for row in rows if row["Department"] == "Finance")
    assert finance["KnownUsers"] == 2
    assert finance["ReturningUsers"] == (2 if include_test_pane else 1)
    assert finance["RepeatUsagePct"] == (100.0 if include_test_pane else 50.0)
    assert finance["MultiAgentUsers"] == (1 if include_test_pane else 0)
    unresolved = next(row for row in rows if row["Attribution"] == "Unresolved")
    assert unresolved["KnownUsers"] == 2
    assert unresolved["ReturningUsers"] == 0


@pytest.mark.parametrize("include_test_pane", [False, True])
def test_unused_agents_respect_environment_and_publication(execute_kql, include_test_pane):
    rows = execute_kql(report_query(REPORT_NAMES[4], synthetic=True, include_test_pane=include_test_pane))
    expected = {("e1", "dormant"), ("e3", "a")}
    if not include_test_pane:
        expected.add(("e1", "test-only"))
    assert {(row["EnvironmentId"], row["DataverseBotId"]) for row in rows} == expected
    assert all(row["Sessions"] == 0 for row in rows)


@pytest.mark.parametrize("include_test_pane", [False, True])
def test_quality_retains_environments_without_sessions(execute_kql, include_test_pane):
    rows = execute_kql(report_query(REPORT_NAMES[5], synthetic=True, include_test_pane=include_test_pane))
    environments = {row["EnvironmentId"]: row for row in rows}
    assert len(rows) == len(environments) == 4
    one = environments["e1"]
    assert one["AllSessions"] == 8
    assert one["TestPaneSessions"] == 1
    assert one["UnknownDesignModeSessions"] == 1
    assert one["ReportSessions"] == (8 if include_test_pane else 7)
    assert one["NoEntraIdSessions"] == 1
    assert one["UnresolvedUserSessions"] == 2
    assert one["ResolvedUserSessions"] == (5 if include_test_pane else 4)
    assert one["MissingAgentIdSessions"] == 1
    assert one["UnmatchedAgentSessions"] == 1
    assert one["AgentMatchedSessions"] == (6 if include_test_pane else 5)
    assert one["ParseFailurePct"] == 50.0
    assert one["TranscriptRows"] == 2
    inventory_only = environments["e3"]
    assert inventory_only["InventoryAgents"] == 1
    assert inventory_only["ReportSessions"] == 0
    assert inventory_only["AgentMatchedPct"] is None
    assert inventory_only["ParseFailurePct"] is None
    assert environments["e4"]["ParseFailurePct"] == 100.0
    assert environments["e4"]["ReportSessions"] == 0
