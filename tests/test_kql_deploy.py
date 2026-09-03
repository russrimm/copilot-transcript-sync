"""Tests for the KQL file splitter used by scripts/deploy_kql.py."""

from __future__ import annotations

import pathlib
import sys

SCRIPTS = pathlib.Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from deploy_kql import split_commands  # noqa: E402

KQL_DIR = pathlib.Path(__file__).resolve().parent.parent / "kql"


def test_splits_consecutive_control_commands():
    text = ".create table A (X: string)\n\n.create table B (Y: string)\n"
    assert split_commands(text) == [
        ".create table A (X: string)",
        ".create table B (Y: string)",
    ]


def test_leading_comments_are_dropped():
    text = "// header comment\n.create table A (X: string)\n"
    assert split_commands(text) == [".create table A (X: string)"]


def test_fenced_payload_is_not_split():
    """A '.' at column zero inside a fenced block must not start a new command."""
    text = (
        '.create-or-alter table T ingestion json mapping "M"\n'
        "```\n"
        "[\n"
        '  {"column":"A","Properties":{"Path":"$.A"}}\n'
        "]\n"
        "```\n"
        "\n"
        ".alter table T policy caching hot = 1d\n"
    )
    commands = split_commands(text)
    assert len(commands) == 2
    assert commands[0].startswith(".create-or-alter table T ingestion")
    assert '"column":"A"' in commands[0]
    assert commands[1] == ".alter table T policy caching hot = 1d"


def test_real_schema_files_parse_into_commands():
    """Guard against the shipped .kql files drifting into an unparseable shape."""
    for path in sorted(KQL_DIR.glob("*.kql")):
        commands = split_commands(path.read_text(encoding="utf-8"))
        assert commands, f"{path.name} produced no commands"
        for command in commands:
            assert command.startswith("."), f"{path.name}: command does not start with '.': {command[:80]!r}"
            # Fences must be balanced within a single command.
            assert command.count("```") % 2 == 0, f"{path.name}: unbalanced fence in {command[:80]!r}"


def test_tables_file_contains_expected_objects():
    text = (KQL_DIR / "01_tables.kql").read_text(encoding="utf-8")
    assert ".create-merge table CopilotTranscriptRaw" in text
    assert "CopilotTranscriptRawMapping" in text


def test_views_file_defines_dedupe_and_projections():
    text = (KQL_DIR / "02_views.kql").read_text(encoding="utf-8")
    assert "materialized-view" in text
    assert "CopilotTranscriptTurn()" in text
    assert "CopilotConversationPair()" in text
