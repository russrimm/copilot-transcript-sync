"""Generate the Copilot Studio analytics Azure Data Explorer dashboard.

Unlike a Power BI ``.pbit``, the dashboard file is a documented JSON format with
a published schema, so this generator validates its own output against
Microsoft's schema rather than guessing at the shape:

    https://dataexplorer.azure.com/static/d/schema/20/dashboard.json

Every tile queries the KQL semantic layer in ``kql/``. No logic is duplicated
here, so ad-hoc analysis and the dashboard cannot drift apart.

    python dashboard/build_dashboard.py
    python dashboard/build_dashboard.py --cluster https://mycluster.eastus.kusto.windows.net

Import the result with **Dashboards -> New dashboard -> Import dashboard from
file** in the Azure Data Explorer web UI.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import uuid

HERE = pathlib.Path(__file__).resolve().parent
DEFAULT_OUT = HERE / "CopilotStudioAnalytics.json"

SCHEMA_URL = "https://dataexplorer.azure.com/static/d/schema/20/dashboard.json"
SCHEMA_VERSION = "20"

# Replaced on import, or with --cluster/--database.
CLUSTER_PLACEHOLDER = "https://your-cluster.region.kusto.windows.net"
DATABASE_PLACEHOLDER = "CopilotTranscripts"

DATA_SOURCE_ID = "11111111-1111-4111-8111-111111111111"

# Deterministic IDs keep the generated file diffable between runs.
NAMESPACE = uuid.UUID("6f9619ff-8b86-d011-b42d-00cf4fc964ff")


def _id(name: str) -> str:
    return str(uuid.uuid5(NAMESPACE, name))


PAGES = ["Overview", "Agents", "Adoption", "Governance"]

# Design-mode sessions are Copilot Studio's test pane, which in a typical
# tenant is the large majority of traffic. Excluded unless the viewer opts in.
DESIGN_MODE_FILTER = "| where IncludeTestPane or not(coalesce(IsDesignMode, false))"
TIME_FILTER = "| where SessionStart between (_startTime .. _endTime)"

STAT_OPTIONS = {
    "multiStat__textSize": "large",
    "multiStat__valueColumn": {"type": "infer"},
    "colorRulesDisabled": True,
}


def session_base() -> str:
    return f"CopilotSession()\n{TIME_FILTER}\n{DESIGN_MODE_FILTER}"


def tile(
    page: str,
    title: str,
    query: str,
    visual_type: str,
    x: int,
    y: int,
    width: int,
    height: int,
    *,
    visual_options: dict | None = None,
    params: list[str] | None = None,
) -> dict:
    return {
        "id": _id(f"{page}/{title}"),
        "title": title,
        "query": query,
        "layout": {"x": x, "y": y, "width": width, "height": height},
        "pageId": _id(f"page/{page}"),
        "visualType": visual_type,
        "dataSourceId": DATA_SOURCE_ID,
        "visualOptions": visual_options or {},
        "usedParamVariables": params
        if params is not None
        else ["_startTime", "_endTime", "IncludeTestPane"],
    }


def stat(page: str, title: str, expression: str, x: int, y: int,
         width: int = 4, height: int = 3) -> dict:
    """A single-number tile over the filtered session set."""
    return tile(
        page, title,
        f"{session_base()}\n| summarize Value = {expression}",
        "card", x, y, width, height,
        visual_options=dict(STAT_OPTIONS),
    )


def build_overview() -> list[dict]:
    page = "Overview"
    return [
        stat(page, "Sessions", "count()", 0, 0),
        stat(page, "People", "dcountif(AadObjectId, isnotempty(AadObjectId))", 4, 0),
        stat(page, "Agents used", "dcount(DataverseBotId)", 8, 0),
        stat(page, "Engagement rate %",
             "round(100.0 * countif(IsEngaged) / count(), 1)", 12, 0),
        stat(page, "Resolution rate %",
             "round(100.0 * countif(IsResolved) / countif(IsEngaged), 1)", 16, 0),
        stat(page, "Median turns", "percentile(TurnCount, 50)", 20, 0),

        tile(page, "Sessions over time",
             f"{session_base()}\n"
             "| summarize Sessions = count(), Engaged = countif(IsEngaged)\n"
             "    by bin(SessionStart, 1d)\n"
             "| order by SessionStart asc",
             "line", 0, 3, 12, 7,
             visual_options={
                 "xColumn": {"type": "infer"},
                 "yColumns": {"type": "infer"},
                 "hideLegend": False,
                 "yColumnTitle": "Sessions",
             }),

        tile(page, "Session outcome",
             f"{session_base()}\n"
             '| extend Outcome = iff(isempty(Outcome), "(none recorded)", Outcome)\n'
             "| summarize Sessions = count() by Outcome\n"
             "| order by Sessions desc",
             "pie", 12, 3, 6, 7,
             visual_options={"hideLegend": False}),

        tile(page, "Sessions by environment",
             f"{session_base()}\n"
             "| summarize Sessions = count() by EnvironmentName\n"
             "| order by Sessions desc",
             "bar", 18, 3, 6, 7,
             visual_options={"hideLegend": True, "xColumnTitle": "Sessions"}),

        tile(page, "Busiest agents",
             f"{session_base()}\n"
             "| summarize Sessions = count(),\n"
             "            People = dcountif(AadObjectId, isnotempty(AadObjectId)),\n"
             "            AvgTurns = round(avg(TurnCount), 1)\n"
             "    by BotName\n"
             "| order by Sessions desc\n"
             "| take 15",
             "table", 0, 10, 12, 7),

        tile(page, "Channels",
             f"{session_base()}\n"
             '| extend ChannelId = iff(isempty(ChannelId), "(unknown)", ChannelId)\n'
             "| summarize Sessions = count() by ChannelId\n"
             "| order by Sessions desc",
             "bar", 12, 10, 12, 7,
             visual_options={"hideLegend": True, "xColumnTitle": "Sessions"}),
    ]


def build_agents() -> list[dict]:
    page = "Agents"
    return [
        tile(page, "Agent performance",
             f"{session_base()}\n"
             "| summarize Sessions = count(),\n"
             "            People = dcountif(AadObjectId, isnotempty(AadObjectId)),\n"
             "            EngagementPct = round(100.0 * countif(IsEngaged) / count(), 1),\n"
             "            ResolutionPct = round(100.0 * countif(IsResolved) / countif(IsEngaged), 1),\n"
             "            EscalationPct = round(100.0 * countif(IsEscalated) / countif(IsEngaged), 1),\n"
             "            AvgTurns = round(avg(TurnCount), 1)\n"
             "    by BotName, EnvironmentName\n"
             "| order by Sessions desc",
             "table", 0, 0, 24, 8),

        tile(page, "Escalation rate by agent",
             f"{session_base()}\n"
             "| summarize Engaged = countif(IsEngaged), Escalated = countif(IsEscalated)\n"
             "    by BotName\n"
             "| where Engaged > 0\n"
             "| extend EscalationPct = round(100.0 * Escalated / Engaged, 1)\n"
             "| project BotName, EscalationPct\n"
             "| top 15 by EscalationPct desc",
             "bar", 0, 8, 12, 7,
             visual_options={"hideLegend": True, "xColumnTitle": "Escalation rate (%)"}),

        tile(page, "Turns per session",
             f"{session_base()}\n"
             "| summarize Sessions = count() by TurnBucket = bin(TurnCount, 2)\n"
             "| order by TurnBucket asc",
             "column", 12, 8, 12, 7,
             visual_options={"hideLegend": True, "xColumnTitle": "Turns"}),
    ]


def build_adoption() -> list[dict]:
    page = "Adoption"
    coverage = (
        f"{session_base()}\n"
        "| summarize Attributable = countif(isnotempty(AadObjectId)), Total = count()"
    )
    return [
        tile(page, "Attribution coverage %",
             f"{coverage}\n| project Value = round(100.0 * Attributable / Total, 1)",
             "card", 0, 0, 6, 3, visual_options=dict(STAT_OPTIONS)),

        stat(page, "People", "dcountif(AadObjectId, isnotempty(AadObjectId))",
             6, 0, 6, 3),

        tile(page, "Sessions per person",
             f"{session_base()}\n"
             "| where isnotempty(AadObjectId)\n"
             "| summarize Sessions = count(), People = dcount(AadObjectId)\n"
             "| project Value = round(1.0 * Sessions / People, 1)",
             "card", 12, 0, 6, 3, visual_options=dict(STAT_OPTIONS)),

        tile(page, "Unattributed sessions",
             f"{coverage}\n| project Value = Total - Attributable",
             "card", 18, 0, 6, 3, visual_options=dict(STAT_OPTIONS)),

        tile(page, "Adoption by department",
             f"{session_base()}\n"
             "| where isnotempty(AadObjectId)\n"
             "| join kind=leftouter (CopilotUser()) on AadObjectId\n"
             '| extend Department = iff(isempty(Department), "(unresolved)", Department)\n'
             "| summarize Sessions = count() by Department\n"
             "| order by Sessions desc",
             "bar", 0, 3, 12, 8,
             visual_options={"hideLegend": True, "xColumnTitle": "Sessions"}),

        tile(page, "Adoption by job title",
             f"{session_base()}\n"
             "| where isnotempty(AadObjectId)\n"
             "| join kind=leftouter (CopilotUser()) on AadObjectId\n"
             '| extend JobTitle = iff(isempty(JobTitle), "(unresolved)", JobTitle)\n'
             "| summarize People = dcount(AadObjectId), Sessions = count() by JobTitle\n"
             "| order by Sessions desc\n"
             "| take 15",
             "table", 12, 3, 12, 8),

        tile(page, "Daily active people",
             f"{session_base()}\n"
             "| where isnotempty(AadObjectId)\n"
             "| summarize People = dcount(AadObjectId) by bin(SessionStart, 1d)\n"
             "| order by SessionStart asc",
             "line", 0, 11, 24, 6,
             visual_options={
                 "xColumn": {"type": "infer"},
                 "yColumns": {"type": "infer"},
                 "hideLegend": True,
                 "yColumnTitle": "People",
             }),
    ]


def build_governance() -> list[dict]:
    page = "Governance"
    # The agent inventory deliberately ignores the session time filter. An
    # unused agent produces no sessions, so it cannot be found by filtering
    # sessions -- that is the whole point of these tiles.
    inventory: list[str] = []
    unused = (
        f"let used = CopilotSession()\n{TIME_FILTER}\n"
        "    | distinct DataverseBotId;\n"
        "CopilotAgent()\n"
        "| where BotId !in (used)"
    )
    return [
        tile(page, "Agents in tenant",
             "CopilotAgent()\n| summarize Value = count()",
             "card", 0, 0, 6, 3,
             visual_options=dict(STAT_OPTIONS), params=inventory),

        tile(page, "Published agents",
             "CopilotAgent()\n| summarize Value = countif(isnotnull(PublishedOn))",
             "card", 6, 0, 6, 3,
             visual_options=dict(STAT_OPTIONS), params=inventory),

        tile(page, "Agents with no sessions",
             f"{unused}\n| summarize Value = count()",
             "card", 12, 0, 6, 3,
             visual_options=dict(STAT_OPTIONS),
             params=["_startTime", "_endTime"]),

        tile(page, "Environments with agents",
             "CopilotAgent()\n| summarize Value = dcount(EnvironmentId)",
             "card", 18, 0, 6, 3,
             visual_options=dict(STAT_OPTIONS), params=inventory),

        tile(page, "Unused agents",
             f"{unused}\n"
             "| project Name, EnvironmentName, StateLabel,\n"
             "          AccessControlPolicyLabel, PublishedOn, CreatedOn\n"
             "| order by CreatedOn desc",
             "table", 0, 3, 14, 8,
             params=["_startTime", "_endTime"]),

        tile(page, "Agents by environment",
             "CopilotAgent()\n"
             "| summarize Agents = count() by EnvironmentName\n"
             "| order by Agents desc",
             "bar", 14, 3, 10, 8,
             visual_options={"hideLegend": True, "xColumnTitle": "Agents"},
             params=inventory),

        tile(page, "Access control policy",
             "CopilotAgent()\n"
             "| extend AccessControlPolicyLabel = iff(isempty(AccessControlPolicyLabel),\n"
             '    "(unspecified)", AccessControlPolicyLabel)\n'
             "| summarize Agents = count() by AccessControlPolicyLabel\n"
             "| order by Agents desc",
             "pie", 0, 11, 8, 6,
             visual_options={"hideLegend": False}, params=inventory),

        tile(page, "Test pane share of traffic",
             f"CopilotSession()\n{TIME_FILTER}\n"
             '| summarize Sessions = count() by Kind = iff(coalesce(IsDesignMode, false),\n'
             '    "Test pane", "Real traffic")',
             "pie", 8, 11, 8, 6,
             visual_options={"hideLegend": False},
             params=["_startTime", "_endTime"]),

        tile(page, "Environment coverage",
             "CopilotTranscriptCoverage(30d)",
             "table", 16, 11, 8, 6, params=[]),
    ]


def build_dashboard(cluster: str, database: str) -> dict:
    tiles = build_overview() + build_agents() + build_adoption() + build_governance()

    return {
        "$schema": SCHEMA_URL,
        "schema_version": SCHEMA_VERSION,
        "id": _id("dashboard"),
        "title": "Copilot Studio analytics",
        "autoRefresh": {"enabled": True, "defaultInterval": "30m", "minInterval": "5m"},
        "dataSources": [
            {
                "id": DATA_SOURCE_ID,
                "name": "Copilot transcripts",
                "scopeId": "kusto",
                "kind": "manual-kusto",
                "clusterUri": cluster,
                "database": database,
            }
        ],
        "pages": [{"id": _id(f"page/{p}"), "name": p} for p in PAGES],
        "tiles": tiles,
        "parameters": [
            {
                "id": _id("param/timerange"),
                "displayName": "Time range",
                "kind": "duration",
                "beginVariableName": "_startTime",
                "endVariableName": "_endTime",
                "showOnPages": {"kind": "all"},
                "defaultValue": {"kind": "dynamic", "count": 30, "unit": "days"},
            },
            {
                "id": _id("param/testpane"),
                "displayName": "Include test pane",
                "kind": "bool",
                "variableName": "IncludeTestPane",
                "selectionType": "single",
                "showOnPages": {"kind": "all"},
                # A non-freetext parameter has to declare the values it offers.
                "dataSource": {
                    "kind": "static",
                    "values": [
                        {"displayText": "Real traffic only", "value": False},
                        {"displayText": "Include test pane", "value": True},
                    ],
                },
                "defaultValue": {"kind": "value", "value": False},
            },
        ],
    }


def validate(dashboard: dict) -> list[str]:
    """Validate against Microsoft's published schema when it is reachable.

    The published schemas identify themselves with absolute paths rather than
    full URLs (``$id`` is ``/static/d/schema/20/tile.json``), so each one is
    registered under its own identifier instead of relying on relative
    resolution.
    """
    try:
        import urllib.parse
        import urllib.request

        import jsonschema
        from referencing import Registry, Resource
    except ImportError:
        return ["skipped: jsonschema not installed"]

    def load(name: str) -> dict:
        target = urllib.parse.urljoin(SCHEMA_URL, name)
        with urllib.request.urlopen(target, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))

    try:
        schemas = [load(n) for n in ("dashboard.json", "tile.json", "parameter.json")]
    except OSError as exc:
        return [f"skipped: could not fetch schema ({exc})"]

    registry = Registry().with_resources(
        (s["$id"], Resource.from_contents(s)) for s in schemas
    )
    validator = jsonschema.Draft202012Validator(schemas[0], registry=registry)
    return [
        f"{'/'.join(str(p) for p in e.absolute_path) or '<root>'}: {e.message}"
        for e in validator.iter_errors(dashboard)
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=pathlib.Path, default=DEFAULT_OUT)
    parser.add_argument("--cluster", default=CLUSTER_PLACEHOLDER)
    parser.add_argument("--database", default=DATABASE_PLACEHOLDER)
    args = parser.parse_args()

    dashboard = build_dashboard(args.cluster, args.database)

    problems = validate(dashboard)
    hard = [p for p in problems if not p.startswith("skipped:")]
    if hard:
        print("Schema validation FAILED:")
        for problem in hard:
            print(f"  {problem}")
        return 1

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(dashboard, indent=2), encoding="utf-8")

    by_page: dict[str, int] = {}
    page_names = {p["id"]: p["name"] for p in dashboard["pages"]}
    for t in dashboard["tiles"]:
        name = page_names[t["pageId"]]
        by_page[name] = by_page.get(name, 0) + 1

    print(f"Wrote {args.out} ({args.out.stat().st_size:,} bytes)")
    print(f"  schema      : {problems[0] if problems else 'validated'}")
    print(f"  pages       : {len(dashboard['pages'])}")
    for name, count in by_page.items():
        print(f"    {name:12s} {count} tiles")
    print(f"  parameters  : {len(dashboard['parameters'])}")
    print(f"  cluster     : {args.cluster}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
