"""Report layout for the Copilot Studio analytics template.

The Report/Layout part is a JSON document whose visual definitions live inside
`config`, itself a JSON *string* rather than a nested object. Each data-bound
visual needs both a `projections` block, naming the fields per visual role, and
a `prototypeQuery` describing the same fields as a semantic query. The two must
agree, or the visual renders empty with no error.

None of this is documented. The shapes here were read off a Microsoft-published
template.
"""

from __future__ import annotations

import json
import uuid

PAGE_WIDTH = 1280
PAGE_HEIGHT = 720

# Muted palette. Escalation and abandonment are the only warm colors, so the eye
# lands on them rather than on whichever bar happens to be tallest.
THEME_PRIMARY = "#2F5D8C"
THEME_SECONDARY = "#6E9BC5"
THEME_WARN = "#C2703D"
THEME_BAD = "#9E3D3D"
THEME_MUTED = "#8A8A8A"


def _name() -> str:
    return uuid.uuid4().hex[:20]


def _select_column(source: str, entity: str, prop: str) -> dict:
    return {
        "Column": {"Expression": {"SourceRef": {"Source": source}}, "Property": prop},
        "Name": f"{entity}.{prop}",
    }


def _select_measure(source: str, entity: str, prop: str) -> dict:
    return {
        "Measure": {"Expression": {"SourceRef": {"Source": source}}, "Property": prop},
        "Name": f"{entity}.{prop}",
    }


def visual(
    visual_type: str,
    x: float,
    y: float,
    width: float,
    height: float,
    *,
    entities: list[str],
    projections: dict[str, list[tuple[str, str, bool]]],
    title: str | None = None,
    subtitle: str | None = None,
    order_by: tuple[str, str, int] | None = None,
    z: int = 0,
) -> dict:
    """Build one visual container.

    ``projections`` maps a visual role to (entity, field, is_measure) tuples.
    ``entities`` lists the tables the query reads, in ``From`` order.
    """
    sources = {entity: f"t{index}" for index, entity in enumerate(entities)}

    proj: dict[str, list[dict]] = {}
    select: list[dict] = []
    seen: set[str] = set()

    for role, fields in projections.items():
        proj[role] = []
        for entity, field, is_measure in fields:
            ref = f"{entity}.{field}"
            proj[role].append({"queryRef": ref})
            if ref in seen:
                continue
            seen.add(ref)
            builder = _select_measure if is_measure else _select_column
            select.append(builder(sources[entity], entity, field))

    # The first field of the first role drives the default sort in most visuals.
    if proj:
        first_role = next(iter(proj))
        if proj[first_role]:
            proj[first_role][0]["active"] = True

    prototype: dict = {
        "Version": 2,
        "From": [{"Name": sources[e], "Entity": e, "Type": 0} for e in entities],
        "Select": select,
    }

    if order_by:
        entity, field, direction = order_by
        prototype["OrderBy"] = [
            {
                "Direction": direction,
                "Expression": {
                    "Measure": {
                        "Expression": {"SourceRef": {"Source": sources[entity]}},
                        "Property": field,
                    }
                },
            }
        ]

    single: dict = {
        "visualType": visual_type,
        "projections": proj,
        "prototypeQuery": prototype,
        "drillFilterOtherVisuals": True,
    }

    vc_objects: dict = {}
    if title:
        vc_objects["title"] = [
            {
                "properties": {
                    "text": {"expr": {"Literal": {"Value": f"'{title}'"}}},
                    "show": {"expr": {"Literal": {"Value": "true"}}},
                    "fontSize": {"expr": {"Literal": {"Value": "12D"}}},
                }
            }
        ]
    if subtitle:
        vc_objects["subTitle"] = [
            {
                "properties": {
                    "text": {"expr": {"Literal": {"Value": f"'{subtitle}'"}}},
                    "show": {"expr": {"Literal": {"Value": "true"}}},
                    "fontSize": {"expr": {"Literal": {"Value": "9D"}}},
                }
            }
        ]
    if vc_objects:
        single["vcObjects"] = vc_objects

    config = {
        "name": _name(),
        "layouts": [
            {
                "id": 0,
                "position": {"x": x, "y": y, "z": z, "width": width, "height": height},
            }
        ],
        "singleVisual": single,
    }

    return {
        "x": x,
        "y": y,
        "z": z,
        "width": width,
        "height": height,
        "config": json.dumps(config),
        "filters": "[]",
    }


def card(x: float, y: float, width: float, height: float, entity: str,
         measure_name: str, title: str, subtitle: str | None = None) -> dict:
    return visual(
        "card", x, y, width, height,
        entities=[entity],
        projections={"Values": [(entity, measure_name, True)]},
        title=title,
        subtitle=subtitle,
    )


def textbox(x: float, y: float, width: float, height: float, text: str,
            *, size: int = 11, bold: bool = False, color: str = "#333333") -> dict:
    """A static text box. Uses the paragraphs structure rather than a query."""
    config = {
        "name": _name(),
        "layouts": [{"id": 0, "position": {"x": x, "y": y, "z": 0,
                                           "width": width, "height": height}}],
        "singleVisual": {
            "visualType": "textbox",
            "objects": {
                "general": [
                    {
                        "properties": {
                            "paragraphs": [
                                {
                                    "textRuns": [
                                        {
                                            "value": text,
                                            "textStyle": {
                                                "fontSize": f"{size}pt",
                                                "fontWeight": "bold" if bold else "normal",
                                                "color": color,
                                            },
                                        }
                                    ],
                                }
                            ]
                        }
                    }
                ]
            },
            "drillFilterOtherVisuals": True,
        },
    }
    return {
        "x": x, "y": y, "z": 0, "width": width, "height": height,
        "config": json.dumps(config), "filters": "[]",
    }


def slicer(x: float, y: float, width: float, height: float,
           entity: str, field: str, title: str) -> dict:
    return visual(
        "slicer", x, y, width, height,
        entities=[entity],
        projections={"Values": [(entity, field, False)]},
        title=title,
    )


def page(name: str, display_name: str, ordinal: int, visuals: list[dict]) -> dict:
    return {
        "id": ordinal,
        "name": f"ReportSection{uuid.uuid4().hex[:20]}",
        "displayName": display_name,
        "filters": "[]",
        "ordinal": ordinal,
        "visualContainers": visuals,
        "config": json.dumps({}),
        "displayOption": 1,
        "width": PAGE_WIDTH,
        "height": PAGE_HEIGHT,
    }


# --------------------------------------------------------------------------
# Pages
# --------------------------------------------------------------------------

def page_overview() -> dict:
    v = [
        textbox(20, 12, 700, 34, "Copilot Studio adoption", size=18, bold=True),
        textbox(20, 44, 900, 26,
                "Tenant-wide session activity. Copilot Studio test pane traffic is excluded "
                "unless the IncludeTestPane parameter is set.",
                size=9, color=THEME_MUTED),

        card(20, 84, 200, 100, "Sessions", "Sessions", "Sessions"),
        card(230, 84, 200, 100, "Sessions", "Users", "People"),
        card(440, 84, 200, 100, "Sessions", "Agents Used", "Agents used"),
        card(650, 84, 200, 100, "Sessions", "Engagement Rate", "Engagement rate"),
        card(860, 84, 200, 100, "Sessions", "Resolution Rate", "Resolution rate"),
        card(1070, 84, 190, 100, "Agents", "Unused Agents", "Unused agents"),

        visual(
            "lineChart", 20, 200, 640, 250,
            entities=["Dates", "Sessions"],
            projections={
                "Category": [("Dates", "Date", False)],
                "Y": [("Sessions", "Sessions", True), ("Sessions", "Engaged Sessions", True)],
            },
            title="Sessions over time",
            subtitle="Total versus engaged",
        ),
        visual(
            "clusteredBarChart", 670, 200, 590, 250,
            entities=["Agents", "Sessions"],
            projections={
                "Category": [("Agents", "Name", False)],
                "Y": [("Sessions", "Sessions", True)],
            },
            title="Busiest agents",
            order_by=("Sessions", "Sessions", 2),
        ),
        visual(
            "donutChart", 20, 462, 380, 240,
            entities=["Sessions"],
            projections={
                "Category": [("Sessions", "Outcome", False)],
                "Y": [("Sessions", "Sessions", True)],
            },
            title="Session outcomes",
        ),
        visual(
            "clusteredColumnChart", 410, 462, 430, 240,
            entities=["Sessions"],
            projections={
                "Category": [("Sessions", "EnvironmentName", False)],
                "Y": [("Sessions", "Sessions", True)],
            },
            title="Sessions by environment",
            order_by=("Sessions", "Sessions", 2),
        ),
        visual(
            "tableEx", 850, 462, 410, 240,
            entities=["Sessions"],
            projections={
                "Values": [
                    ("Sessions", "ChannelId", False),
                    ("Sessions", "Sessions", True),
                    ("Sessions", "Avg Turns", True),
                ]
            },
            title="Channels",
        ),
    ]
    return page("overview", "Overview", 0, v)


def page_agents() -> dict:
    v = [
        textbox(20, 12, 700, 34, "Agent performance", size=18, bold=True),
        textbox(20, 44, 900, 26,
                "Resolution and escalation are shares of engaged sessions, because an agent "
                "cannot resolve a question nobody asked.",
                size=9, color=THEME_MUTED),

        slicer(20, 84, 240, 300, "Environments", "EnvironmentName", "Environment"),
        slicer(20, 394, 240, 308, "Agents", "StateLabel", "Agent state"),

        visual(
            "tableEx", 275, 84, 985, 320,
            entities=["Agents", "Sessions"],
            projections={
                "Values": [
                    ("Agents", "Name", False),
                    ("Agents", "EnvironmentName", False),
                    ("Sessions", "Sessions", True),
                    ("Sessions", "Users", True),
                    ("Sessions", "Engagement Rate", True),
                    ("Sessions", "Resolution Rate", True),
                    ("Sessions", "Escalation Rate", True),
                    ("Sessions", "Avg Turns", True),
                ]
            },
            title="Agent scorecard",
            order_by=("Sessions", "Sessions", 2),
        ),
        visual(
            "clusteredBarChart", 275, 414, 490, 288,
            entities=["Agents", "Sessions"],
            projections={
                "Category": [("Agents", "Name", False)],
                "Y": [("Sessions", "Escalation Rate", True)],
            },
            title="Escalation rate by agent",
            subtitle="High values suggest gaps in agent knowledge",
            order_by=("Sessions", "Escalation Rate", 2),
        ),
        visual(
            "scatterChart", 775, 414, 485, 288,
            entities=["Agents", "Sessions"],
            projections={
                "Category": [("Agents", "Name", False)],
                "X": [("Sessions", "Sessions", True)],
                "Y": [("Sessions", "Resolution Rate", True)],
                "Size": [("Sessions", "Users", True)],
            },
            title="Volume versus resolution",
            subtitle="Bottom right is busy and unhelpful",
        ),
    ]
    return page("agents", "Agents", 1, v)


def page_adoption() -> dict:
    v = [
        textbox(20, 12, 700, 34, "Adoption by organization", size=18, bold=True),
        textbox(20, 44, 1000, 40,
                "Only authenticated channels record who the user was, so these breakdowns "
                "describe the attributable share of traffic. Check the coverage card before "
                "reading the rest of this page. Empty unless SYNC_USERS is enabled.",
                size=9, color=THEME_MUTED),

        card(20, 92, 220, 100, "Sessions", "Attribution Coverage", "Attribution coverage",
             "Share of sessions tied to a person"),
        card(250, 92, 220, 100, "Sessions", "Users", "People"),
        card(480, 92, 220, 100, "Sessions", "Sessions per User", "Sessions per person"),

        visual(
            "clusteredBarChart", 20, 208, 620, 260,
            entities=["Users", "Sessions"],
            projections={
                "Category": [("Users", "Department", False)],
                "Y": [("Sessions", "Sessions", True)],
            },
            title="Sessions by department",
            order_by=("Sessions", "Sessions", 2),
        ),
        visual(
            "clusteredBarChart", 650, 208, 610, 260,
            entities=["Users", "Sessions"],
            projections={
                "Category": [("Users", "JobTitle", False)],
                "Y": [("Sessions", "Users", True)],
            },
            title="People by job title",
            order_by=("Sessions", "Users", 2),
        ),
        visual(
            "tableEx", 20, 478, 1240, 224,
            entities=["Users", "Sessions"],
            projections={
                "Values": [
                    ("Users", "Department", False),
                    ("Sessions", "Users", True),
                    ("Sessions", "Sessions", True),
                    ("Sessions", "Sessions per User", True),
                    ("Sessions", "Engagement Rate", True),
                    ("Sessions", "Resolution Rate", True),
                ]
            },
            title="Department detail",
            order_by=("Sessions", "Sessions", 2),
        ),
    ]
    return page("adoption", "Adoption", 2, v)


def page_governance() -> dict:
    v = [
        textbox(20, 12, 700, 34, "Governance", size=18, bold=True),
        textbox(20, 44, 1000, 26,
                "Agent inventory across every environment, including agents nobody uses. "
                "An unused agent produces no transcripts, so only the agent sync reveals it.",
                size=9, color=THEME_MUTED),

        card(20, 84, 220, 100, "Agents", "Total Agents", "Agents in tenant"),
        card(250, 84, 220, 100, "Agents", "Published Agents", "Published"),
        card(480, 84, 220, 100, "Agents", "Unused Agents", "Never used"),
        card(710, 84, 220, 100, "Agents", "Unused Agent Share", "Unused share"),
        card(940, 84, 320, 100, "Agents", "Agents Without Authentication",
             "Open access policy", "Access control set to Any"),

        visual(
            "clusteredColumnChart", 20, 200, 620, 240,
            entities=["Environments", "Agents"],
            projections={
                "Category": [("Environments", "EnvironmentName", False)],
                "Y": [("Agents", "Total Agents", True), ("Agents", "Unused Agents", True)],
            },
            title="Agents per environment",
            subtitle="Total versus never used",
        ),
        visual(
            "donutChart", 650, 200, 300, 240,
            entities=["Agents"],
            projections={
                "Category": [("Agents", "AccessControlPolicyLabel", False)],
                "Y": [("Agents", "Total Agents", True)],
            },
            title="Access control policy",
        ),
        visual(
            "donutChart", 960, 200, 300, 240,
            entities=["Agents"],
            projections={
                "Category": [("Agents", "StateLabel", False)],
                "Y": [("Agents", "Total Agents", True)],
            },
            title="Agent state",
        ),
        visual(
            "tableEx", 20, 450, 1240, 252,
            entities=["Agents", "Sessions"],
            projections={
                "Values": [
                    ("Agents", "Name", False),
                    ("Agents", "EnvironmentName", False),
                    ("Agents", "StateLabel", False),
                    ("Agents", "AccessControlPolicyLabel", False),
                    ("Agents", "PublishedOn", False),
                    ("Agents", "AgentAgeDays", False),
                    ("Sessions", "Sessions", True),
                ]
            },
            title="Agent inventory",
        ),
    ]
    return page("governance", "Governance", 3, v)


def build_layout() -> dict:
    return {
        "id": 0,
        "resourcePackages": [
            {
                "resourcePackage": {
                    "disabled": False,
                    "items": [{"name": "CY24SU10", "path": "BaseThemes/CY24SU10.json", "type": 202}],
                    "name": "SharedResources",
                    "type": 2,
                }
            }
        ],
        "sections": [
            page_overview(),
            page_agents(),
            page_adoption(),
            page_governance(),
        ],
        "config": json.dumps(
            {
                "version": "5.43",
                "themeCollection": {"baseTheme": {"name": "CY24SU10",
                                                  "version": "5.43",
                                                  "type": 2}},
                "activeSectionIndex": 0,
                "defaultDrillFilterOtherVisuals": True,
                "settings": {"useStylableVisualContainerHeader": True},
            }
        ),
        "layoutOptimization": 0,
    }
