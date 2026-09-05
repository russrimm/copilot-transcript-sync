"""Generate the Copilot Studio analytics Power BI template (.pbit).

A .pbit is a ZIP of parts. Several things about the format are easy to get wrong
and are not documented by Microsoft. Each was read off a Microsoft-published
template rather than guessed, and each one on its own makes Power BI Desktop
reject the file with only "We couldn't open your file... it may be corrupted":

* Most parts are UTF-16LE with **no** byte order mark, but ``[Content_Types].xml``
  is UTF-8 **with** a BOM.
* Every part needs a matching ``Default``/``Override`` entry in
  ``[Content_Types].xml``, and no entry may name a part that is absent.
* ``DataMashup`` is not just a framed ZIP. It is five consecutive sections:
  a 4-byte version, then four length-prefixed blocks -- the package ZIP, a
  permissions XML, a metadata block, and permission bindings. Writing only the
  version and the package ZIP truncates the part.
* The metadata block is itself structured: a 4-byte version, a length-prefixed
  UTF-8 XML document, then a length-prefixed content blob (an empty ZIP is
  valid).
* ``SecurityBindings`` and the permission bindings are DPAPI blobs tied to the
  machine that wrote them. A template cannot carry meaningful ones, so this
  writes an empty binding and omits the part.

One further value is version-sensitive rather than structural. The ``Version``
part must match what the installed Power BI Desktop writes. An older value is
still accepted and the file opens, but it triggers an upgrade pass that leaves
the parameter values you supply sitting in an ``UnappliedChanges`` part instead
of being applied, and the document never finishes loading. ``1.30`` was read
back out of a file Power BI produced itself, not guessed.

    python powerbi/build_template.py
    python powerbi/build_template.py --out dist/CopilotStudioAnalytics.pbit
"""

from __future__ import annotations

import argparse
import io
import json
import pathlib
import re
import struct
import sys
import zipfile

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from layout import build_layout  # noqa: E402
from model import build_model  # noqa: E402

DEFAULT_OUT = HERE / "CopilotStudioAnalytics.pbit"
SECTION_M = HERE / "Section1.m"

# Power BI writes most parts as UTF-16LE without a BOM, but [Content_Types].xml
# as UTF-8 with one.
UTF16 = "utf-16-le"
BOM = "\ufeff"

TEMPLATE_DESCRIPTION = (
    "Copilot Studio adoption, agent performance, organizational adoption and agent "
    "governance, over an Azure Data Explorer archive of conversation transcripts "
    "collected from every Power Platform environment in the tenant."
)

CONTENT_TYPES = (
    BOM + '<?xml version="1.0" encoding="utf-8"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="json" ContentType="" />'
    '<Override PartName="/Version" ContentType="" />'
    '<Override PartName="/DataMashup" ContentType="" />'
    '<Override PartName="/DataModelSchema" ContentType="" />'
    '<Override PartName="/DiagramLayout" ContentType="" />'
    '<Override PartName="/Report/Layout" ContentType="" />'
    '<Override PartName="/Settings" ContentType="application/json" />'
    '<Override PartName="/Metadata" ContentType="application/json" />'
    '<Override PartName="/DiagramState" ContentType="" />'
    "</Types>"
)

MASHUP_CONTENT_TYPES = (
    BOM + '<?xml version="1.0" encoding="utf-8"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="xml" ContentType="text/xml" />'
    '<Default Extension="m" ContentType="application/x-ms-m" />'
    "</Types>"
)

MASHUP_PACKAGE_XML = (
    BOM + '<?xml version="1.0" encoding="utf-8"?>'
    '<Package xmlns:xsd="http://www.w3.org/2001/XMLSchema" '
    'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
    "<Version>2.130.1052.0</Version>"
    "<MinVersion>1.5.3296.0</MinVersion>"
    "<Culture>en-US</Culture>"
    "</Package>"
)

MASHUP_PERMISSIONS = (
    BOM + '<?xml version="1.0" encoding="utf-8"?>'
    '<PermissionList xmlns:xsd="http://www.w3.org/2001/XMLSchema" '
    'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
    "<CanEvaluateFuturePackages>false</CanEvaluateFuturePackages>"
    "<FirewallEnabled>true</FirewallEnabled>"
    "</PermissionList>"
)

# Per-formula Power Query state. This is not optional bookkeeping: without an
# entry marking each parameter as not loaded, Power BI treats all nine shared
# formulas as tables to load, tries to materialize the four parameters as data
# tables, and spins on "Syncing schema..." burning a full core until it
# eventually dies. The values mirror a Microsoft-published template.
PARAMETERS = (
    ("ClusterUri", "Text"),
    ("DatabaseName", "Text"),
    ("LookbackDays", "Number"),
    ("IncludeTestPane", "Logical"),
)

TABLE_QUERIES = ("Sessions", "Agents", "Users", "Environments", "Dates")


def _metadata_item(path: str, entries: str) -> str:
    return (
        "<Item><ItemLocation><ItemType>Formula</ItemType>"
        f"<ItemPath>Section1/{path}</ItemPath></ItemLocation>"
        f"<StableEntries>{entries}</StableEntries></Item>"
    )


def build_mashup_metadata_xml() -> str:
    items = [
        "<Item><ItemLocation><ItemType>AllFormulas</ItemType><ItemPath /></ItemLocation>"
        "<StableEntries>"
        '<Entry Type="IsTypeDetectionEnabled" Value="sTrue" />'
        '<Entry Type="RunBackgroundAnalysis" Value="sFalse" />'
        "</StableEntries></Item>"
    ]

    for name, result_type in PARAMETERS:
        items.append(
            _metadata_item(
                name,
                '<Entry Type="LoadedToAnalysisServices" Value="l0" />'
                '<Entry Type="LoadToReportDisabled" Value="l1" />'
                f'<Entry Type="ResultType" Value="s{result_type}" />',
            )
        )

    for name in TABLE_QUERIES:
        items.append(
            _metadata_item(
                name,
                '<Entry Type="IsDirectQuery" Value="l0" />'
                '<Entry Type="LoadedToAnalysisServices" Value="l1" />'
                '<Entry Type="LoadToReportDisabled" Value="l0" />'
                '<Entry Type="ResultType" Value="sTable" />',
            )
        )

    return (
        BOM + '<?xml version="1.0" encoding="utf-8"?>'
        '<LocalPackageMetadataFile xmlns:xsd="http://www.w3.org/2001/XMLSchema" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
        "<Items>" + "".join(items) + "</Items></LocalPackageMetadataFile>"
    )

# End-of-central-directory record for a ZIP with no entries.
EMPTY_ZIP = bytes.fromhex("504b0506" + "00" * 18)


def _section(payload: bytes) -> bytes:
    """Length-prefix a DataMashup section."""
    return struct.pack("<I", len(payload)) + payload


def build_data_mashup(section_m: str) -> bytes:
    """Frame the Power Query section as a DataMashup part.

    Layout: uint32 version, then four length-prefixed sections -- package ZIP,
    permissions, metadata, permission bindings. Omitting the trailing three
    sections truncates the part and Power BI reports the whole file as corrupt.
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as package:
        package.writestr("Config/Package.xml", MASHUP_PACKAGE_XML)
        package.writestr("[Content_Types].xml", MASHUP_CONTENT_TYPES)
        package.writestr("Formulas/Section1.m", section_m)

    metadata_xml = build_mashup_metadata_xml().encode("utf-8")
    metadata = struct.pack("<I", 0) + _section(metadata_xml) + _section(EMPTY_ZIP)

    return (
        struct.pack("<I", 0)
        + _section(buffer.getvalue())
        + _section(MASHUP_PERMISSIONS.encode("utf-8"))
        + _section(metadata)
        # Permission bindings are a DPAPI blob tied to the authoring machine.
        # A redistributable template cannot carry a usable one.
        + _section(b"")
    )


def build_settings() -> dict:
    return {
        "Version": 1,
        "ReportSettings": {
            "ShowHiddenFields": False,
            "IsRelationshipAutodetectionEnabled": False,
            "IsParallelQueryLoadingEnabled": True,
            "IsAutoRecoveryEnabledForThisFile": True,
            "IsQnaEnabledForThisFile": True,
            "UserConsentsToCompositeModels": False,
            "UserConsentsToQnaForLiveConnect": False,
        },
        "QueriesSettings": {
            "TypeDetectionEnabled": True,
            "RelationshipImportEnabled": True,
            "RelationshipRefreshEnabled": False,
            "RunBackgroundAnalysis": False,
            "Version": "2.130.1052.0",
        },
    }


def build_metadata() -> dict:
    return {
        "Version": 5,
        "AutoCreatedRelationships": [],
        "FileDescription": TEMPLATE_DESCRIPTION,
        "CreatedFrom": "Desktop",
    }


def build_diagram_layout() -> dict:
    """Model diagram positions, so the relationship view is legible on open."""
    return {
        "version": "1.1.0",
        "diagrams": [
            {
                "ordinal": 0,
                "scrollPosition": {"x": 0, "y": 0},
                "nodes": [
                    {"location": {"x": 420, "y": 40}, "nodeIndex": "Sessions",
                     "size": {"height": 320, "width": 240}, "zIndex": 0},
                    {"location": {"x": 60, "y": 40}, "nodeIndex": "Agents",
                     "size": {"height": 280, "width": 220}, "zIndex": 1},
                    {"location": {"x": 800, "y": 40}, "nodeIndex": "Users",
                     "size": {"height": 240, "width": 220}, "zIndex": 2},
                    {"location": {"x": 60, "y": 380}, "nodeIndex": "Environments",
                     "size": {"height": 140, "width": 220}, "zIndex": 3},
                    {"location": {"x": 420, "y": 420}, "nodeIndex": "Dates",
                     "size": {"height": 200, "width": 220}, "zIndex": 4},
                ],
                "name": "All tables",
                "zoomValue": 100,
                "pinKeyFieldsToTop": False,
                "showExtraHeaderInfo": False,
                "hideKeyFieldsWhenCollapsed": False,
                "tablesLocked": False,
            }
        ],
        "selectedDiagram": "All tables",
        "defaultDiagram": "All tables",
    }


def write_template(out_path: pathlib.Path) -> pathlib.Path:
    if not SECTION_M.exists():
        raise SystemExit(f"Missing Power Query definitions: {SECTION_M}")

    section_m = SECTION_M.read_text(encoding="utf-8")

    # Every shared formula needs a metadata entry. A query with no entry is
    # treated as a table to load, so a parameter that is missing here makes
    # Power BI hang trying to materialize it.
    declared = {name for name, _ in PARAMETERS} | set(TABLE_QUERIES)
    actual = set(re.findall(r"(?m)^shared\s+([A-Za-z_][\w]*)\s*=", section_m))
    if actual != declared:
        raise SystemExit(
            "Section1.m and the metadata declarations disagree.\n"
            f"  only in Section1.m : {sorted(actual - declared)}\n"
            f"  only in metadata   : {sorted(declared - actual)}"
        )

    out_path.parent.mkdir(parents=True, exist_ok=True)

    def utf16(text: str) -> bytes:
        return text.encode(UTF16)

    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as pbit:
        pbit.writestr("Version", utf16("1.30"))
        pbit.writestr("[Content_Types].xml", CONTENT_TYPES)
        pbit.writestr("DataMashup", build_data_mashup(section_m))
        pbit.writestr(
            "DataModelSchema",
            utf16(json.dumps(build_model(), indent=2, ensure_ascii=False)),
        )
        pbit.writestr(
            "DiagramLayout",
            utf16(json.dumps(build_diagram_layout(), ensure_ascii=False)),
        )
        pbit.writestr(
            "Report/Layout",
            utf16(json.dumps(build_layout(), ensure_ascii=False)),
        )
        pbit.writestr("Settings", utf16(json.dumps(build_settings(), ensure_ascii=False)))
        pbit.writestr("Metadata", utf16(json.dumps(build_metadata(), ensure_ascii=False)))
        pbit.writestr("DiagramState", utf16(json.dumps({"Version": 0, "Data": []})))

    return out_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=pathlib.Path, default=DEFAULT_OUT)
    args = parser.parse_args()

    path = write_template(args.out)
    size = path.stat().st_size

    model = build_model()
    layout = build_layout()
    tables = model["model"]["tables"]
    measures = sum(len(t.get("measures", [])) for t in tables)
    visuals = sum(len(s["visualContainers"]) for s in layout["sections"])

    print(f"Wrote {path} ({size:,} bytes)")
    print(f"  tables       : {len(tables)}")
    print(f"  measures     : {measures}")
    print(f"  relationships: {len(model['model']['relationships'])}")
    print(f"  parameters   : {len(model['model']['expressions'])}")
    print(f"  pages        : {len(layout['sections'])}")
    print(f"  visuals      : {visuals}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
