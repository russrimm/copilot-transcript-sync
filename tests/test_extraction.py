"""Tests for the pure extraction and projection logic."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from copilot_transcript_sync.dataverse import (
    TranscriptRow,
    _conversation_id,
    _loads,
    _odata_datetime,
    _safe_int,
)
from copilot_transcript_sync.powerplatform import (
    PowerPlatformEnvironment,
    _as_environment,
    is_transcript_eligible,
)
from copilot_transcript_sync.watermarks import _parse


# --------------------------------------------------------------------------
# Conversation ID recovery
# --------------------------------------------------------------------------


def test_conversation_id_strips_bot_suffix():
    name = "8YYe8iif49ZKkycZLe7HUO_198eca5f-1145-4ae6-8c08-835d884a8688"
    bot_id = "198eca5f-1145-4ae6-8c08-835d884a8688"
    assert _conversation_id(name, bot_id) == "8YYe8iif49ZKkycZLe7HUO"


def test_conversation_id_preserves_underscores_in_conversation():
    """A conversation ID may contain underscores; only the BotId suffix is trimmed."""
    bot_id = "198eca5f-1145-4ae6-8c08-835d884a8688"
    name = f"abc_def_ghi_{bot_id}"
    assert _conversation_id(name, bot_id) == "abc_def_ghi"


def test_conversation_id_falls_back_when_bot_id_unknown():
    name = "conversation123_198eca5f-1145-4ae6-8c08-835d884a8688"
    assert _conversation_id(name, "") == "conversation123"


def test_conversation_id_handles_empty_name():
    assert _conversation_id("", "anything") == ""


def test_conversation_id_without_separator_returns_name():
    assert _conversation_id("nodelimiter", "") == "nodelimiter"


# --------------------------------------------------------------------------
# Embedded JSON parsing
# --------------------------------------------------------------------------


def test_loads_parses_embedded_json():
    value, error = _loads('{"BotId":"abc","BatchId":2}')
    assert error is None
    assert value == {"BotId": "abc", "BatchId": 2}


def test_loads_reports_error_without_raising():
    value, error = _loads("{not valid json")
    assert value is None
    assert error is not None


def test_loads_passes_through_empty():
    assert _loads(None) == (None, None)
    assert _loads("") == (None, None)


def test_loads_passes_through_non_string():
    payload = [{"type": "message"}]
    assert _loads(payload) == (payload, None)


# --------------------------------------------------------------------------
# OData filter formatting
# --------------------------------------------------------------------------


def test_odata_datetime_is_utc_zulu():
    value = datetime(2026, 3, 1, 14, 30, 15, tzinfo=timezone.utc)
    assert _odata_datetime(value) == "2026-03-01T14:30:15Z"


def test_odata_datetime_converts_non_utc_input():
    from datetime import timedelta

    eastern = timezone(timedelta(hours=-5))
    value = datetime(2026, 3, 1, 9, 30, 15, tzinfo=eastern)
    assert _odata_datetime(value) == "2026-03-01T14:30:15Z"


def test_safe_int_handles_bad_input():
    assert _safe_int("123") == 123
    assert _safe_int(None) is None
    assert _safe_int("abc") is None


# --------------------------------------------------------------------------
# BAP environment projection
# --------------------------------------------------------------------------


def _bap_environment(**overrides):
    payload = {
        "name": "11112222-3333-4444-5555-666677778888",
        "properties": {
            "displayName": "Contoso Production",
            "environmentSku": "Production",
            "isDefault": False,
            "databaseType": "CommonDataService",
            "linkedEnvironmentMetadata": {
                "instanceUrl": "https://org0fadb1dd.crm.dynamics.com/",
                "instanceApiUrl": "https://org0fadb1dd.api.crm.dynamics.com",
                "instanceState": "Ready",
                "resourceId": "aaaabbbb-0000-cccc-1111-dddd2222eeee",
            },
        },
    }
    payload["properties"].update(overrides)
    return payload


def test_as_environment_projects_dataverse_org():
    environment = _as_environment(_bap_environment())
    assert environment is not None
    assert environment.display_name == "Contoso Production"
    assert environment.environment_type == "Production"
    # Trailing slash must be stripped so the token scope is well formed.
    assert environment.environment_url == "https://org0fadb1dd.crm.dynamics.com"
    assert environment.api_base == "https://org0fadb1dd.crm.dynamics.com/api/data/v9.2"


def test_as_environment_returns_none_without_dataverse():
    payload = {"name": "abc", "properties": {"displayName": "No database"}}
    assert _as_environment(payload) is None


def test_as_environment_returns_none_when_instance_url_blank():
    payload = _bap_environment(linkedEnvironmentMetadata={"instanceUrl": "  "})
    assert _as_environment(payload) is None


# --------------------------------------------------------------------------
# Environment eligibility
# --------------------------------------------------------------------------


def _environment(environment_type="Production", state="Ready"):
    return PowerPlatformEnvironment(
        environment_id="env-1",
        display_name="Env",
        environment_url="https://org.crm.dynamics.com",
        organization_id="org-1",
        environment_type=environment_type,
        state=state,
        is_default=False,
    )


@pytest.mark.parametrize(
    "environment_type", ["Production", "Sandbox", "Trial", "Developer", "Teams", "Default"]
)
def test_no_environment_type_is_excluded_by_default(environment_type):
    """Every type is queried unless explicitly opted out.

    Microsoft documents that Developer environments never persist transcripts.
    That is wrong: a Developer environment was measured holding 31 transcripts,
    more than every Production environment in the same tenant combined. Skipping
    a type on documentation alone silently loses data.
    """
    assert is_transcript_eligible(_environment(environment_type), frozenset())


@pytest.mark.parametrize("environment_type", ["Developer", "developer", "Teams", "TEAMS"])
def test_types_can_be_excluded_by_configuration(environment_type):
    excluded = frozenset({"developer", "teams"})
    assert not is_transcript_eligible(_environment(environment_type), excluded)


def test_configured_exclusions_are_honored():
    excluded = frozenset({"trial"})
    assert not is_transcript_eligible(_environment("Trial"), excluded)
    assert is_transcript_eligible(_environment("Production"), excluded)


def test_non_ready_instances_are_skipped():
    assert not is_transcript_eligible(_environment("Production", state="NotReady"), frozenset())


# --------------------------------------------------------------------------
# Ingestion payload
# --------------------------------------------------------------------------


def test_bot_identifiers_are_kept_separate():
    """The runtime BotId and the Dataverse foreign key are different values.

    metadata.BotId is a runtime agent identifier that does not join to the bot
    table. _bot_conversationtranscriptid_value is the actual foreign key. An
    earlier version coalesced them, which silently broke the agent join.
    """
    from copilot_transcript_sync.dataverse import DataverseTranscriptReader

    environment = PowerPlatformEnvironment(
        environment_id="env-1",
        display_name="Env",
        environment_url="https://org.crm.dynamics.com",
        organization_id="org-1",
        environment_type="Production",
        state="Ready",
        is_default=False,
    )
    reader = DataverseTranscriptReader(None, None, environment, 25)

    row = reader._project(
        {
            "conversationtranscriptid": "t1",
            "name": "conv_runtimebot",
            "metadata": '{"BotId":"runtimebot","BotName":"cr123_myAgent"}',
            "_bot_conversationtranscriptid_value": "dataverse-bot-guid",
            "content": "[]",
        },
        "2026-05-01T00:00:00+00:00",
    )

    assert row.bot_id == "runtimebot"
    assert row.dataverse_bot_id == "dataverse-bot-guid"
    # BotName carries the schema name, not a display name.
    assert row.bot_name == "cr123_myAgent"
    assert row.conversation_id == "conv"


def test_transcript_row_serializes_expected_columns():
    row = TranscriptRow(
        environment_id="env-1",
        environment_name="Contoso",
        environment_url="https://org.crm.dynamics.com",
        organization_id="org-1",
        conversation_transcript_id="28eccb77-0000-4a63-985f-ffaaadd6f391",
        name="conv_bot",
        conversation_id="conv",
        bot_id="bot",
        dataverse_bot_id="dv-bot",
        bot_name="Test Bot",
        batch_id="2",
        aad_tenant_id="tenant",
        schema_type="powervirtualagents",
        schema_version="1.0",
        conversation_start_time="2026-04-19T20:39:09Z",
        created_on="2026-04-20T02:40:13Z",
        modified_on="2026-04-20T02:40:13Z",
        version_number=1234,
        metadata={"BotId": "bot", "BatchId": 2},
        content=[{"type": "message", "text": "hello"}],
        content_parse_error=None,
        ingested_at="2026-04-20T03:00:00+00:00",
    )

    payload = json.loads(row.to_json_line())

    assert payload["ConversationTranscriptId"] == "28eccb77-0000-4a63-985f-ffaaadd6f391"
    assert payload["Content"] == [{"type": "message", "text": "hello"}]
    assert payload["Metadata"]["BatchId"] == 2
    assert payload["VersionNumber"] == 1234
    assert payload["ContentParseError"] is None


def test_transcript_row_json_line_has_no_newlines():
    """Rows are concatenated into a single JSON array, so embedded newlines would corrupt it."""
    row = TranscriptRow(
        environment_id="env-1",
        environment_name="Contoso",
        environment_url="https://org.crm.dynamics.com",
        organization_id="org-1",
        conversation_transcript_id="id",
        name="n",
        conversation_id="c",
        bot_id="b",
        dataverse_bot_id="dv-b",
        bot_name="Bot",
        batch_id="",
        aad_tenant_id="t",
        schema_type="powervirtualagents",
        schema_version="1.0",
        conversation_start_time=None,
        created_on=None,
        modified_on=None,
        version_number=None,
        metadata=None,
        content=[{"text": "line one\nline two"}],
        content_parse_error=None,
        ingested_at="2026-04-20T03:00:00+00:00",
    )
    assert "\n" not in row.to_json_line()


# --------------------------------------------------------------------------
# Watermark parsing
# --------------------------------------------------------------------------


def test_watermark_parse_accepts_zulu_and_offset():
    assert _parse("2026-04-20T02:40:13Z") == datetime(2026, 4, 20, 2, 40, 13, tzinfo=timezone.utc)
    assert _parse("2026-04-20T02:40:13+00:00") == datetime(
        2026, 4, 20, 2, 40, 13, tzinfo=timezone.utc
    )


def test_watermark_parse_assumes_utc_when_naive():
    assert _parse("2026-04-20T02:40:13") == datetime(2026, 4, 20, 2, 40, 13, tzinfo=timezone.utc)


def test_watermark_parse_returns_none_on_garbage():
    assert _parse("not a date") is None
    assert _parse("") is None

