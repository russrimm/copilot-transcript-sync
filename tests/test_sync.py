"""Orchestration tests for run_sync, using fakes for every external dependency."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest

from copilot_transcript_sync import sync as sync_module
from copilot_transcript_sync.dataverse import TranscriptRow
from copilot_transcript_sync.powerplatform import PowerPlatformEnvironment
from copilot_transcript_sync.settings import Settings

NOW = datetime(2026, 5, 1, 12, 0, 0, tzinfo=timezone.utc)


def _settings(**overrides) -> Settings:
    defaults = dict(
        tenant_id="tenant",
        app_client_id="app",
        uami_client_id="uami",
        adx_cluster_uri="https://cluster.kusto.windows.net",
        adx_ingest_uri="https://ingest-cluster.kusto.windows.net",
        adx_database="CopilotTranscripts",
        adx_raw_table="CopilotTranscriptRaw",
        watermark_table_endpoint="https://st.table.core.windows.net",
        watermark_table_name="SyncWatermarks",
        initial_backfill_days=30,
        watermark_lookback_minutes=120,
        dataverse_page_size=25,
        max_concurrent_environments=4,
        auto_provision_app_user=True,
        read_only_role_name="",
        excluded_environment_types=frozenset(),
    )
    defaults.update(overrides)
    return Settings(**defaults)


def _environment(env_id: str, name: str, env_type: str = "Production") -> PowerPlatformEnvironment:
    return PowerPlatformEnvironment(
        environment_id=env_id,
        display_name=name,
        environment_url=f"https://{env_id}.crm.dynamics.com",
        organization_id=f"org-{env_id}",
        environment_type=env_type,
        state="Ready",
        is_default=False,
    )


def _row(env: PowerPlatformEnvironment, index: int, created_on: datetime) -> TranscriptRow:
    return TranscriptRow(
        environment_id=env.environment_id,
        environment_name=env.display_name,
        environment_url=env.environment_url,
        organization_id=env.organization_id,
        conversation_transcript_id=f"{env.environment_id}-{index}",
        name=f"conv{index}_bot",
        conversation_id=f"conv{index}",
        bot_id="bot",
        bot_name="Bot",
        batch_id="0",
        aad_tenant_id="tenant",
        schema_type="powervirtualagents",
        schema_version="1.0",
        conversation_start_time=created_on.isoformat(),
        created_on=created_on.isoformat(),
        modified_on=created_on.isoformat(),
        version_number=index,
        metadata={"BotId": "bot"},
        content=[{"type": "message", "text": "hi"}],
        content_parse_error=None,
        ingested_at=NOW.isoformat(),
    )


class FakeWatermarkStore:
    def __init__(self, *_args, **_kwargs):
        self.written: dict[str, datetime] = {}
        self.closed = False

    def read_start_time(self, environment_id: str) -> datetime:
        return NOW - timedelta(days=30)

    def write(self, environment_id: str, environment_name: str, watermark: datetime) -> None:
        self.written[environment_id] = watermark

    def close(self) -> None:
        self.closed = True


class FakeSink:
    def __init__(self, *_args, **_kwargs):
        self.rows: list[TranscriptRow] = []
        self.rows_ingested = 0
        self.flushes = 0
        self.closed = False

    def add(self, row: TranscriptRow) -> None:
        self.rows.append(row)
        self.rows_ingested += 1

    def flush(self) -> None:
        self.flushes += 1

    def close(self) -> None:
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()


class FakeAdmin:
    def __init__(self, environments):
        self._environments = environments
        self.provisioned: list[str] = []

    def list_environments(self):
        return self._environments

    def ensure_application_user(self, environment_id: str, app_client_id: str) -> bool:
        self.provisioned.append(environment_id)
        return True


@pytest.fixture
def wired(monkeypatch):
    """Replace every external dependency of run_sync with a fake."""
    created: dict[str, object] = {}

    monkeypatch.setattr(sync_module, "power_platform_credential", lambda s: object())
    monkeypatch.setattr(sync_module, "azure_credential", lambda s: object())
    monkeypatch.setattr(sync_module, "TokenProvider", lambda credential: object())

    def _make_watermarks(**_kwargs):
        store = FakeWatermarkStore()
        created["watermarks"] = store
        return store

    def _make_sink(**_kwargs):
        sink = FakeSink()
        created["sink"] = sink
        return sink

    monkeypatch.setattr(sync_module, "WatermarkStore", _make_watermarks)
    monkeypatch.setattr(sync_module, "AdxTranscriptSink", _make_sink)
    return created


def _install_reader(monkeypatch, behavior):
    """behavior: environment_id -> list[TranscriptRow] or an exception to raise."""

    class FakeReader:
        def __init__(self, _client, _tokens, environment, _page_size):
            self._environment = environment

        def read_since(self, since):
            outcome = behavior(self._environment.environment_id, since)
            if isinstance(outcome, Exception):
                raise outcome
            yield from outcome

    monkeypatch.setattr(sync_module, "DataverseTranscriptReader", FakeReader)


def test_syncs_all_eligible_environments(monkeypatch, wired):
    environments = [_environment("env1", "Prod"), _environment("env2", "Sandbox", "Sandbox")]
    monkeypatch.setattr(
        sync_module, "PowerPlatformAdminClient", lambda *_a: FakeAdmin(environments)
    )
    _install_reader(
        monkeypatch,
        lambda env_id, _since: [
            _row(next(e for e in environments if e.environment_id == env_id), i, NOW - timedelta(hours=i))
            for i in range(3)
        ],
    )

    result = sync_module.run_sync(_settings())

    assert result.environments_discovered == 2
    assert result.environments_synced == 2
    assert result.environments_failed == 0
    assert result.rows_ingested == 6
    assert wired["watermarks"].closed is True


def test_watermark_advances_to_latest_created_on(monkeypatch, wired):
    env = _environment("env1", "Prod")
    monkeypatch.setattr(sync_module, "PowerPlatformAdminClient", lambda *_a: FakeAdmin([env]))

    latest = NOW - timedelta(minutes=5)
    rows = [
        _row(env, 0, NOW - timedelta(hours=3)),
        _row(env, 1, latest),
        _row(env, 2, NOW - timedelta(hours=1)),
    ]
    _install_reader(monkeypatch, lambda _env_id, _since: rows)

    sync_module.run_sync(_settings())

    # The maximum createdon wins, not the last row read.
    assert wired["watermarks"].written["env1"] == latest


def test_watermark_not_advanced_when_no_rows(monkeypatch, wired):
    env = _environment("env1", "Prod")
    monkeypatch.setattr(sync_module, "PowerPlatformAdminClient", lambda *_a: FakeAdmin([env]))
    _install_reader(monkeypatch, lambda _env_id, _since: [])

    result = sync_module.run_sync(_settings())

    assert wired["watermarks"].written == {}
    assert result.environments[0].skipped is True
    assert result.environments_synced == 0


def test_excluded_environments_are_never_queried(monkeypatch, wired):
    """Exclusions come from configuration only, and are honored when set."""
    environments = [
        _environment("env1", "Prod"),
        _environment("dev1", "Dev", "Developer"),
        _environment("teams1", "Teams", "Teams"),
    ]
    admin = FakeAdmin(environments)
    monkeypatch.setattr(sync_module, "PowerPlatformAdminClient", lambda *_a: admin)

    queried: list[str] = []

    def behavior(env_id, _since):
        queried.append(env_id)
        return []

    _install_reader(monkeypatch, behavior)

    result = sync_module.run_sync(
        _settings(excluded_environment_types=frozenset({"developer", "teams"}))
    )

    assert queried == ["env1"]
    assert admin.provisioned == ["env1"]
    assert result.environments_discovered == 3


def test_developer_environments_are_queried_by_default(monkeypatch, wired):
    """Regression: Developer environments hold transcripts despite the docs.

    A Developer environment was measured holding more transcripts than every
    Production environment in the same tenant combined, so nothing is excluded
    unless the operator opts in.
    """
    environments = [
        _environment("env1", "Prod"),
        _environment("dev1", "Dev", "Developer"),
        _environment("teams1", "Teams", "Teams"),
    ]
    admin = FakeAdmin(environments)
    monkeypatch.setattr(sync_module, "PowerPlatformAdminClient", lambda *_a: admin)

    queried: list[str] = []

    def behavior(env_id, _since):
        queried.append(env_id)
        env = next(e for e in environments if e.environment_id == env_id)
        return [_row(env, 0, NOW)]

    _install_reader(monkeypatch, behavior)

    result = sync_module.run_sync(_settings())

    assert sorted(queried) == ["dev1", "env1", "teams1"]
    assert sorted(admin.provisioned) == ["dev1", "env1", "teams1"]
    assert result.rows_ingested == 3


def test_one_failing_environment_does_not_abort_the_run(monkeypatch, wired):
    environments = [
        _environment("env1", "Prod"),
        _environment("env2", "Broken"),
        _environment("env3", "Other"),
    ]
    monkeypatch.setattr(
        sync_module, "PowerPlatformAdminClient", lambda *_a: FakeAdmin(environments)
    )

    def behavior(env_id, _since):
        if env_id == "env2":
            return PermissionError("application user missing")
        env = next(e for e in environments if e.environment_id == env_id)
        return [_row(env, 0, NOW)]

    _install_reader(monkeypatch, behavior)

    result = sync_module.run_sync(_settings())

    assert result.environments_synced == 2
    assert result.environments_failed == 1
    assert result.rows_ingested == 2
    failed = next(e for e in result.environments if e.environment_id == "env2")
    assert "application user missing" in failed.error
    # A failed environment must not move its watermark forward.
    assert "env2" not in wired["watermarks"].written


def test_transport_failure_is_isolated_per_environment(monkeypatch, wired):
    environments = [_environment("env1", "Prod"), _environment("env2", "Other")]
    monkeypatch.setattr(
        sync_module, "PowerPlatformAdminClient", lambda *_a: FakeAdmin(environments)
    )

    def behavior(env_id, _since):
        if env_id == "env1":
            return httpx.ConnectError("boom")
        return [_row(environments[1], 0, NOW)]

    _install_reader(monkeypatch, behavior)

    result = sync_module.run_sync(_settings())

    assert result.environments_failed == 1
    assert result.environments_synced == 1


def test_app_user_provisioning_can_be_disabled(monkeypatch, wired):
    env = _environment("env1", "Prod")
    admin = FakeAdmin([env])
    monkeypatch.setattr(sync_module, "PowerPlatformAdminClient", lambda *_a: admin)
    _install_reader(monkeypatch, lambda _env_id, _since: [])

    sync_module.run_sync(_settings(auto_provision_app_user=False))

    assert admin.provisioned == []
