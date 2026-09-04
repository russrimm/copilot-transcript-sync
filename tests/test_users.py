"""Tests for Entra object ID extraction and Microsoft Graph user resolution."""

from __future__ import annotations

import json

from copilot_transcript_sync.dataverse import extract_aad_object_ids
from copilot_transcript_sync.users import UserRow, _project

OBJECT_ID = "8ea5fd1f-1290-4c75-839a-822ddac4fba5"
OTHER_ID = "1234abcd-0000-1111-2222-333344445555"


# --------------------------------------------------------------------------
# Object ID extraction
# --------------------------------------------------------------------------


def test_extracts_object_id_from_activities_object():
    content = {
        "activities": [
            {"type": "trace", "from": {"id": "hashed", "role": 0}},
            {"type": "event", "from": {"id": "hashed", "role": 1, "aadObjectId": OBJECT_ID}},
        ]
    }
    assert extract_aad_object_ids(content) == {OBJECT_ID}


def test_extracts_from_bare_array():
    content = [{"from": {"role": 1, "aadObjectId": OBJECT_ID}}]
    assert extract_aad_object_ids(content) == {OBJECT_ID}


def test_extracts_from_double_encoded_content():
    """The third content encoding must not hide users."""
    inner = json.dumps({"activities": [{"from": {"aadObjectId": OBJECT_ID}}]})
    assert extract_aad_object_ids({"text": inner}) == {OBJECT_ID}


def test_hashed_from_id_is_never_treated_as_an_object_id():
    """from.id is hashed and must not be mistaken for a resolvable identifier."""
    content = {"activities": [{"from": {"id": "32a64dd9-11db-1b19-6d8a-7fde530fc3f9", "role": 1}}]}
    assert extract_aad_object_ids(content) == set()


def test_deduplicates_across_activities():
    content = {
        "activities": [
            {"from": {"aadObjectId": OBJECT_ID}},
            {"from": {"aadObjectId": OBJECT_ID}},
            {"from": {"aadObjectId": OTHER_ID}},
        ]
    }
    assert extract_aad_object_ids(content) == {OBJECT_ID, OTHER_ID}


def test_malformed_content_yields_nothing_rather_than_raising():
    for payload in (None, "", "not json", 42, {"activities": "nope"}, {"text": "{broken"}):
        assert extract_aad_object_ids(payload) == set()


def test_ignores_blank_and_non_string_object_ids():
    content = {
        "activities": [
            {"from": {"aadObjectId": "   "}},
            {"from": {"aadObjectId": None}},
            {"from": {"aadObjectId": 12345}},
            {"from": "not a dict"},
            "not a dict",
        ]
    }
    assert extract_aad_object_ids(content) == set()


# --------------------------------------------------------------------------
# Graph projection
# --------------------------------------------------------------------------


def test_projects_resolved_user():
    row = _project(
        OBJECT_ID,
        {
            "id": OBJECT_ID,
            "displayName": "Ada Lovelace",
            "userPrincipalName": "ada@contoso.com",
            "jobTitle": "Engineer",
            "department": "Research",
            "accountEnabled": True,
        },
        "2026-05-01T00:00:00+00:00",
    )
    assert row.resolved is True
    assert row.department == "Research"
    assert row.job_title == "Engineer"
    assert row.account_enabled is True


def test_unresolved_user_still_produces_a_row():
    """A deleted or external user must not silently vanish from the dimension."""
    row = _project(OBJECT_ID, None, "2026-05-01T00:00:00+00:00")
    assert row.resolved is False
    assert row.aad_object_id == OBJECT_ID
    assert row.department == ""


def test_user_row_serializes_without_newlines():
    row = UserRow(
        aad_object_id=OBJECT_ID,
        display_name="Ada\nLovelace",
        user_principal_name="ada@contoso.com",
        mail="ada@contoso.com",
        job_title="Engineer",
        department="Research",
        company_name="Contoso",
        office_location="B1",
        city="London",
        country="UK",
        usage_location="GB",
        employee_type="Employee",
        account_enabled=True,
        resolved=True,
        ingested_at="2026-05-01T00:00:00+00:00",
    )
    line = row.to_json_line()
    assert "\n" not in line
    payload = json.loads(line)
    assert payload["AadObjectId"] == OBJECT_ID
    assert payload["Department"] == "Research"
    assert payload["Resolved"] is True
