section Section1;

// ---------------------------------------------------------------------------
// Copilot Studio analytics - Power Query definitions
//
// Every table is a single Kusto query against the semantic layer created by
// kql/*.kql. The heavy lifting stays in Azure Data Explorer, so the model is a
// thin star schema over functions rather than raw transcripts.
//
// Parameters carry IsParameterQuery metadata, which is what makes Power BI
// prompt for them when the template is opened.
// ---------------------------------------------------------------------------

shared ClusterUri = "https://your-cluster.region.kusto.windows.net" meta [IsParameterQuery=true, Type="Text", IsParameterQueryRequired=true];

shared DatabaseName = "CopilotTranscripts" meta [IsParameterQuery=true, Type="Text", IsParameterQueryRequired=true];

shared LookbackDays = 90 meta [IsParameterQuery=true, Type="Number", IsParameterQueryRequired=true];

// Copilot Studio test pane traffic is a maker testing their own agent, not
// adoption. Excluded by default. Turn it on only to investigate, and read the
// Attribution visuals to judge how much of the picture it represents.
shared IncludeTestPane = false meta [IsParameterQuery=true, Type="Logical", IsParameterQueryRequired=true];

// ---------------------------------------------------------------------------
// Fact: one row per Copilot Studio session.
// ---------------------------------------------------------------------------

shared Sessions = let
    DesignMode = if IncludeTestPane then "true" else "false",
    Query = "CopilotSession()"
        & "#(lf)| where SessionStart > ago(" & Number.ToText(LookbackDays) & "d)"
        & "#(lf)| where " & DesignMode & " or not(coalesce(IsDesignMode, false))"
        & "#(lf)| extend SessionDateKey = startofday(SessionStart)"
        & "#(lf)| extend DurationSeconds = iff(isnotnull(SessionEnd) and isnotnull(SessionStart), todouble(datetime_diff('second', SessionEnd, SessionStart)), todouble(0))"
        & "#(lf)| project EnvironmentId, EnvironmentName, ConversationId, ConversationTranscriptId,"
        & " DataverseBotId, BotName, SessionStart, SessionEnd, SessionDateKey, DurationSeconds,"
        & " SessionType, IsEngaged, Outcome, OutcomeReason, IsResolved, IsEscalated, IsAbandoned,"
        & " ImpliedSuccess, TurnCount, IsDesignMode, Locale, AadObjectId, ChannelId",
    Source = AzureDataExplorer.Contents(ClusterUri, DatabaseName, Query, [MaxRows=null, MaxSize=null, NoTruncate=null, AdditionalSetStatements=null])
in
    Source;

// ---------------------------------------------------------------------------
// Dimension: agents.
//
// Includes agents with no sessions at all, which is the point: an unused agent
// produces no transcripts, so transcript data alone cannot reveal it.
// ---------------------------------------------------------------------------

shared Agents = let
    Query = "CopilotAgent()"
        & "#(lf)| project BotId, Name, SchemaName, EnvironmentId, EnvironmentName,"
        & " StateLabel, AccessControlPolicyLabel, AuthenticationMode, IsManaged, Template,"
        & " PublishedOn, CreatedOn, ModifiedOn, OwnerId"
        & "#(lf)| extend IsPublished = isnotnull(PublishedOn)"
        & "#(lf)| extend AgentAgeDays = todouble(datetime_diff('day', now(), CreatedOn))",
    Source = AzureDataExplorer.Contents(ClusterUri, DatabaseName, Query, [MaxRows=null, MaxSize=null, NoTruncate=null, AdditionalSetStatements=null])
in
    Source;

// ---------------------------------------------------------------------------
// Dimension: users.
//
// Empty unless SYNC_USERS is enabled on the Function, which is deliberate --
// it resolves personal data. The union with an empty datatable keeps the query
// valid, and the model working, when the table does not exist yet.
// ---------------------------------------------------------------------------

shared Users = let
    Query = "union isfuzzy=true"
        & "#(lf)  (CopilotUser() | project AadObjectId, DisplayName, UserPrincipalName, JobTitle, Department, CompanyName, OfficeLocation, City, Country, EmployeeType, Resolved),"
        & "#(lf)  (datatable(AadObjectId:string, DisplayName:string, UserPrincipalName:string, JobTitle:string, Department:string, CompanyName:string, OfficeLocation:string, City:string, Country:string, EmployeeType:string, Resolved:bool) [])"
        & "#(lf)| extend Department = iff(isempty(Department), '(unknown)', Department)"
        & "#(lf)| extend JobTitle = iff(isempty(JobTitle), '(unknown)', JobTitle)",
    Source = AzureDataExplorer.Contents(ClusterUri, DatabaseName, Query, [MaxRows=null, MaxSize=null, NoTruncate=null, AdditionalSetStatements=null])
in
    Source;

// ---------------------------------------------------------------------------
// Dimension: environments.
// ---------------------------------------------------------------------------

shared Environments = let
    Query = "CopilotAgent()"
        & "#(lf)| summarize AgentCount = count() by EnvironmentId, EnvironmentName"
        & "#(lf)| order by EnvironmentName asc",
    Source = AzureDataExplorer.Contents(ClusterUri, DatabaseName, Query, [MaxRows=null, MaxSize=null, NoTruncate=null, AdditionalSetStatements=null])
in
    Source;

// ---------------------------------------------------------------------------
// Dimension: dates.
//
// Generated locally rather than queried, so trend visuals keep a continuous
// axis across days with no sessions.
// ---------------------------------------------------------------------------

shared Dates = let
    StartDate = Date.AddDays(Date.From(DateTime.FixedLocalNow()), -LookbackDays),
    EndDate = Date.From(DateTime.FixedLocalNow()),
    DayCount = Duration.Days(EndDate - StartDate) + 1,
    DayList = List.Dates(StartDate, DayCount, #duration(1,0,0,0)),
    AsTable = Table.FromList(DayList, Splitter.SplitByNothing(), {"DateOnly"}),
    Typed = Table.TransformColumnTypes(AsTable, {{"DateOnly", type date}}),
    WithKey = Table.AddColumn(Typed, "Date", each DateTime.From([DateOnly]), type datetime),
    WithYear = Table.AddColumn(WithKey, "Year", each Date.Year([DateOnly]), Int64.Type),
    WithMonthNum = Table.AddColumn(WithYear, "MonthNumber", each Date.Month([DateOnly]), Int64.Type),
    WithMonth = Table.AddColumn(WithMonthNum, "Month", each Date.ToText([DateOnly], "MMM yyyy"), type text),
    WithWeek = Table.AddColumn(WithMonth, "WeekStart", each DateTime.From(Date.StartOfWeek([DateOnly], Day.Monday)), type datetime),
    WithDow = Table.AddColumn(WithWeek, "DayOfWeek", each Date.ToText([DateOnly], "ddd"), type text),
    WithDowNum = Table.AddColumn(WithDow, "DayOfWeekNumber", each Date.DayOfWeek([DateOnly], Day.Monday), Int64.Type),
    Final = Table.RemoveColumns(WithDowNum, {"DateOnly"})
in
    Final;
