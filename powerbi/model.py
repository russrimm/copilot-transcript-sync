"""Generate the Copilot Studio analytics Power BI template.

A .pbit is a ZIP of parts, most of which are UTF-16LE encoded JSON with no BOM.
The awkward one is DataMashup: a 4-byte version, a 4-byte length, then a nested
ZIP holding the Power Query definitions. None of that is documented by
Microsoft, so the layout here was read off a real Microsoft-published template.

    python powerbi/build_template.py
    python powerbi/build_template.py --out dist/CopilotStudioAnalytics.pbit
"""

from __future__ import annotations

import json
import uuid

# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------

def column(name: str, data_type: str, *, hidden: bool = False,
           format_string: str | None = None, sort_by: str | None = None,
           summarize: str | None = "none", description: str | None = None) -> dict:
    col: dict = {
        "name": name,
        "dataType": data_type,
        "sourceColumn": name,
        "summarizeBy": summarize or "none",
    }
    if hidden:
        col["isHidden"] = True
    if format_string:
        col["formatString"] = format_string
    if sort_by:
        col["sortByColumn"] = sort_by
    if description:
        col["description"] = description
    return col


def measure(name: str, expression: str, *, format_string: str | None = None,
            folder: str | None = None, description: str | None = None) -> dict:
    m: dict = {"name": name, "expression": expression}
    if format_string:
        m["formatString"] = format_string
    if folder:
        m["displayFolder"] = folder
    if description:
        m["description"] = description
    return m


def table(name: str, columns: list[dict], m_expression: str, *,
          measures: list[dict] | None = None, description: str | None = None,
          is_date_table: bool = False) -> dict:
    t: dict = {
        "name": name,
        "columns": columns,
        "partitions": [
            {
                "name": f"{name}-partition",
                "mode": "import",
                "source": {"type": "m", "expression": m_expression},
            }
        ],
    }
    if measures:
        t["measures"] = measures
    if description:
        t["description"] = description
    if is_date_table:
        t["dataCategory"] = "Time"
    return t


SESSION_MEASURES = [
    measure("Sessions", "COUNTROWS('Sessions')", format_string="#,0",
            folder="Volume", description="Total sessions in the selected context."),
    measure("Conversations", "DISTINCTCOUNT('Sessions'[ConversationId])",
            format_string="#,0", folder="Volume"),
    measure("Agents Used", "DISTINCTCOUNT('Sessions'[DataverseBotId])",
            format_string="#,0", folder="Volume"),
    measure(
        "Users",
        "CALCULATE(DISTINCTCOUNT('Sessions'[AadObjectId]), 'Sessions'[AadObjectId] <> \"\")",
        format_string="#,0", folder="Volume",
        description="Distinct people. Only authenticated channels write a user identifier, "
                    "so this undercounts when unauthenticated traffic is present.",
    ),
    measure("Engaged Sessions", "CALCULATE([Sessions], 'Sessions'[IsEngaged] = TRUE())",
            format_string="#,0", folder="Engagement"),
    measure(
        "Engagement Rate",
        "DIVIDE([Engaged Sessions], [Sessions])",
        format_string="0.0%", folder="Engagement",
        description="Share of sessions where the user actually interacted, rather than "
                    "opening the agent and leaving.",
    ),
    measure("Resolved Sessions", "CALCULATE([Sessions], 'Sessions'[IsResolved] = TRUE())",
            format_string="#,0", folder="Outcomes"),
    measure("Escalated Sessions", "CALCULATE([Sessions], 'Sessions'[IsEscalated] = TRUE())",
            format_string="#,0", folder="Outcomes"),
    measure("Abandoned Sessions", "CALCULATE([Sessions], 'Sessions'[IsAbandoned] = TRUE())",
            format_string="#,0", folder="Outcomes"),
    measure(
        "Resolution Rate", "DIVIDE([Resolved Sessions], [Engaged Sessions])",
        format_string="0.0%", folder="Outcomes",
        description="Resolved as a share of ENGAGED sessions. Unengaged sessions are "
                    "excluded because an agent cannot resolve a question nobody asked.",
    ),
    measure("Escalation Rate", "DIVIDE([Escalated Sessions], [Engaged Sessions])",
            format_string="0.0%", folder="Outcomes"),
    measure("Abandon Rate", "DIVIDE([Abandoned Sessions], [Engaged Sessions])",
            format_string="0.0%", folder="Outcomes"),
    measure("Avg Turns", "AVERAGE('Sessions'[TurnCount])", format_string="0.0",
            folder="Depth"),
    measure("Median Duration (sec)",
            "MEDIANX('Sessions', 'Sessions'[DurationSeconds])",
            format_string="0.0", folder="Depth"),
    measure("Sessions per User", "DIVIDE([Sessions], [Users])", format_string="0.0",
            folder="Depth"),
    measure(
        "Test Pane Sessions",
        "CALCULATE(COUNTROWS('Sessions'), 'Sessions'[IsDesignMode] = TRUE())",
        format_string="#,0", folder="Attribution",
        description="Sessions from the Copilot Studio test pane. Zero unless the "
                    "IncludeTestPane parameter was set to true.",
    ),
    measure(
        "Attributable Sessions",
        "CALCULATE(COUNTROWS('Sessions'), 'Sessions'[AadObjectId] <> \"\")",
        format_string="#,0", folder="Attribution",
    ),
    measure(
        "Attribution Coverage",
        "DIVIDE([Attributable Sessions], [Sessions])",
        format_string="0.0%", folder="Attribution",
        description="Share of sessions that can be tied to a person. Departmental "
                    "breakdowns only describe this share of traffic.",
    ),
    measure("Sessions (Previous Period)",
            "CALCULATE([Sessions], DATEADD('Dates'[Date], -1, MONTH))",
            format_string="#,0", folder="Trend"),
    measure(
        "Sessions MoM %",
        "DIVIDE([Sessions] - [Sessions (Previous Period)], [Sessions (Previous Period)])",
        format_string="0.0%", folder="Trend",
    ),
]

AGENT_MEASURES = [
    measure("Total Agents", "COUNTROWS('Agents')", format_string="#,0",
            folder="Inventory"),
    measure("Published Agents",
            "CALCULATE(COUNTROWS('Agents'), 'Agents'[IsPublished] = TRUE())",
            format_string="#,0", folder="Inventory"),
    measure(
        "Agents With Sessions",
        "CALCULATE(DISTINCTCOUNT('Sessions'[DataverseBotId]), ALLSELECTED('Sessions'))",
        format_string="#,0", folder="Inventory",
    ),
    measure(
        "Unused Agents",
        "VAR UsedIds = CALCULATETABLE(VALUES('Sessions'[DataverseBotId]), ALL('Dates'))\n"
        "RETURN COUNTROWS(FILTER('Agents', NOT('Agents'[BotId] IN UsedIds)))",
        format_string="#,0", folder="Inventory",
        description="Agents with no sessions in the loaded window. Transcript data alone "
                    "cannot show these, because an unused agent produces no transcripts.",
    ),
    measure(
        "Unused Agent Share", "DIVIDE([Unused Agents], [Total Agents])",
        format_string="0.0%", folder="Inventory",
    ),
    measure(
        "Agents Without Authentication",
        "CALCULATE(COUNTROWS('Agents'), 'Agents'[AccessControlPolicyLabel] = \"Any\")",
        format_string="#,0", folder="Governance",
        description="Agents whose access control policy allows anyone. Worth reviewing "
                    "even when usage is low.",
    ),
]


def build_model() -> dict:
    sessions = table(
        "Sessions",
        [
            column("EnvironmentId", "string", hidden=True),
            column("EnvironmentName", "string"),
            column("ConversationId", "string", hidden=True),
            column("ConversationTranscriptId", "string", hidden=True),
            column("DataverseBotId", "string", hidden=True),
            column("BotName", "string", description="Agent schema name from the transcript. "
                                                    "Prefer Agents[Name] for display."),
            column("SessionStart", "dateTime", format_string="General Date"),
            column("SessionEnd", "dateTime", format_string="General Date", hidden=True),
            column("SessionDateKey", "dateTime", hidden=True),
            column("DurationSeconds", "double", format_string="0.0"),
            column("SessionType", "string"),
            column("IsEngaged", "boolean"),
            column("Outcome", "string"),
            column("OutcomeReason", "string"),
            column("IsResolved", "boolean"),
            column("IsEscalated", "boolean"),
            column("IsAbandoned", "boolean"),
            column("ImpliedSuccess", "boolean"),
            column("TurnCount", "int64", format_string="#,0"),
            column("IsDesignMode", "boolean",
                   description="True for Copilot Studio test pane traffic: a maker testing "
                               "their own agent, not adoption."),
            column("Locale", "string"),
            column("AadObjectId", "string", hidden=True),
            column("ChannelId", "string"),
        ],
        "let\n    Source = Sessions\nin\n    Source",
        measures=SESSION_MEASURES,
        description="One row per Copilot Studio session.",
    )

    agents = table(
        "Agents",
        [
            column("BotId", "string", hidden=True),
            column("Name", "string", description="Agent display name, which exists only in "
                                                 "the Dataverse bot table."),
            column("SchemaName", "string"),
            column("EnvironmentId", "string", hidden=True),
            column("EnvironmentName", "string"),
            column("StateLabel", "string"),
            column("AccessControlPolicyLabel", "string"),
            column("AuthenticationMode", "int64", summarize="none"),
            column("IsManaged", "boolean"),
            column("Template", "string"),
            column("PublishedOn", "dateTime", format_string="General Date"),
            column("CreatedOn", "dateTime", format_string="General Date"),
            column("ModifiedOn", "dateTime", format_string="General Date"),
            column("OwnerId", "string", hidden=True),
            column("IsPublished", "boolean"),
            column("AgentAgeDays", "double", format_string="#,0"),
        ],
        "let\n    Source = Agents\nin\n    Source",
        measures=AGENT_MEASURES,
        description="Every Copilot Studio agent in the tenant, including unused ones.",
    )

    users = table(
        "Users",
        [
            column("AadObjectId", "string", hidden=True),
            column("DisplayName", "string"),
            column("UserPrincipalName", "string"),
            column("JobTitle", "string"),
            column("Department", "string"),
            column("CompanyName", "string"),
            column("OfficeLocation", "string"),
            column("City", "string"),
            column("Country", "string"),
            column("EmployeeType", "string"),
            column("Resolved", "boolean"),
        ],
        "let\n    Source = Users\nin\n    Source",
        description="Entra attributes for people seen in transcripts. Empty unless "
                    "SYNC_USERS is enabled. Contains personal data.",
    )

    environments = table(
        "Environments",
        [
            column("EnvironmentId", "string", hidden=True),
            column("EnvironmentName", "string"),
            column("AgentCount", "int64", format_string="#,0"),
        ],
        "let\n    Source = Environments\nin\n    Source",
    )

    dates = table(
        "Dates",
        [
            column("Date", "dateTime", format_string="General Date"),
            column("Year", "int64", format_string="0"),
            column("MonthNumber", "int64", hidden=True),
            column("Month", "string", sort_by="MonthNumber"),
            column("WeekStart", "dateTime", format_string="Short Date"),
            column("DayOfWeek", "string", sort_by="DayOfWeekNumber"),
            column("DayOfWeekNumber", "int64", hidden=True),
        ],
        "let\n    Source = Dates\nin\n    Source",
        is_date_table=True,
        description="Local date dimension, so trends keep a continuous axis on days "
                    "with no sessions.",
    )

    relationships = [
        {
            "name": "Sessions_Agents",
            "fromTable": "Sessions",
            "fromColumn": "DataverseBotId",
            "toTable": "Agents",
            "toColumn": "BotId",
            "crossFilteringBehavior": "bothDirections",
        },
        {
            "name": "Sessions_Dates",
            "fromTable": "Sessions",
            "fromColumn": "SessionDateKey",
            "toTable": "Dates",
            "toColumn": "Date",
        },
        {
            "name": "Sessions_Users",
            "fromTable": "Sessions",
            "fromColumn": "AadObjectId",
            "toTable": "Users",
            "toColumn": "AadObjectId",
        },
        {
            "name": "Agents_Environments",
            "fromTable": "Agents",
            "fromColumn": "EnvironmentId",
            "toTable": "Environments",
            "toColumn": "EnvironmentId",
        },
    ]

    parameters = [
        {
            "name": "ClusterUri",
            "kind": "m",
            "expression": '"https://your-cluster.region.kusto.windows.net" '
                          'meta [IsParameterQuery=true, Type="Text", IsParameterQueryRequired=true]',
        },
        {
            "name": "DatabaseName",
            "kind": "m",
            "expression": '"CopilotTranscripts" '
                          'meta [IsParameterQuery=true, Type="Text", IsParameterQueryRequired=true]',
        },
        {
            "name": "LookbackDays",
            "kind": "m",
            "expression": '90 meta [IsParameterQuery=true, Type="Number", IsParameterQueryRequired=true]',
        },
        {
            "name": "IncludeTestPane",
            "kind": "m",
            "expression": 'false meta [IsParameterQuery=true, Type="Logical", IsParameterQueryRequired=true]',
        },
    ]

    return {
        "name": str(uuid.uuid4()),
        "compatibilityLevel": 1520,
        "model": {
            "culture": "en-US",
            "dataAccessOptions": {
                "legacyRedirects": True,
                "returnErrorValuesAsNull": True,
            },
            "defaultPowerBIDataSourceVersion": "powerBI_V3",
            "sourceQueryCulture": "en-US",
            "tables": [sessions, agents, users, environments, dates],
            "relationships": relationships,
            "expressions": parameters,
            "annotations": [
                {"name": "PBI_QueryOrder",
                 "value": json.dumps(["ClusterUri", "DatabaseName", "LookbackDays",
                                      "IncludeTestPane", "Sessions", "Agents", "Users",
                                      "Environments", "Dates"])},
                {"name": "__PBI_TimeIntelligenceEnabled", "value": "0"},
            ],
        },
    }
