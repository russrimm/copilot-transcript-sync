"""Orchestration: discover environments, extract transcripts, ingest into ADX."""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

import httpx

from .adx import AdxTranscriptSink
from .credentials import azure_credential, power_platform_credential
from .dataverse import DataverseTranscriptReader
from .http import DEFAULT_TIMEOUT, TokenProvider
from .powerplatform import (
    PowerPlatformAdminClient,
    PowerPlatformEnvironment,
    is_transcript_eligible,
)
from .settings import Settings
from .watermarks import WatermarkStore

logger = logging.getLogger(__name__)


@dataclass
class EnvironmentResult:
    environment_id: str
    environment_name: str
    environment_type: str
    rows: int = 0
    skipped: bool = False
    error: str | None = None


@dataclass
class SyncResult:
    started_at: str
    finished_at: str | None = None
    environments_discovered: int = 0
    environments_synced: int = 0
    environments_failed: int = 0
    rows_ingested: int = 0
    environments: list[EnvironmentResult] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


def _parse_created_on(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def run_sync(settings: Settings) -> SyncResult:
    """Run one full pass across every eligible environment in the tenant."""
    result = SyncResult(started_at=datetime.now(timezone.utc).isoformat())

    pp_tokens = TokenProvider(power_platform_credential(settings))
    az_credential = azure_credential(settings)

    watermarks = WatermarkStore(
        endpoint=settings.watermark_table_endpoint,
        table_name=settings.watermark_table_name,
        credential=az_credential,
        initial_backfill_days=settings.initial_backfill_days,
        lookback_minutes=settings.watermark_lookback_minutes,
    )

    try:
        with httpx.Client(timeout=DEFAULT_TIMEOUT, follow_redirects=True) as http_client:
            admin = PowerPlatformAdminClient(http_client, pp_tokens)
            environments = admin.list_environments()
            result.environments_discovered = len(environments)

            eligible = [
                environment
                for environment in environments
                if is_transcript_eligible(environment, settings.excluded_environment_types)
            ]

            with AdxTranscriptSink(
                ingest_uri=settings.adx_ingest_uri,
                database=settings.adx_database,
                table=settings.adx_raw_table,
                credential=az_credential,
            ) as sink:
                for environment in eligible:
                    outcome = _sync_environment(
                        environment=environment,
                        settings=settings,
                        admin=admin,
                        http_client=http_client,
                        pp_tokens=pp_tokens,
                        sink=sink,
                        watermarks=watermarks,
                    )
                    result.environments.append(outcome)
                    if outcome.error:
                        result.environments_failed += 1
                    elif not outcome.skipped:
                        result.environments_synced += 1

                # Flush before reading the total so the count reflects real work.
                sink.flush()
                result.rows_ingested = sink.rows_ingested
    finally:
        watermarks.close()

    result.finished_at = datetime.now(timezone.utc).isoformat()
    logger.info(
        "Sync complete: %d/%d environments synced, %d failed, %d rows ingested.",
        result.environments_synced,
        result.environments_discovered,
        result.environments_failed,
        result.rows_ingested,
    )
    return result


def _sync_environment(
    *,
    environment: PowerPlatformEnvironment,
    settings: Settings,
    admin: PowerPlatformAdminClient,
    http_client: httpx.Client,
    pp_tokens: TokenProvider,
    sink: AdxTranscriptSink,
    watermarks: WatermarkStore,
) -> EnvironmentResult:
    outcome = EnvironmentResult(
        environment_id=environment.environment_id,
        environment_name=environment.display_name,
        environment_type=environment.environment_type,
    )

    if settings.auto_provision_app_user:
        admin.ensure_application_user(environment.environment_id, settings.app_client_id)

    since = watermarks.read_start_time(environment.environment_id)
    reader = DataverseTranscriptReader(
        http_client, pp_tokens, environment, settings.dataverse_page_size
    )

    highest_created_on: datetime | None = None
    rows = 0

    try:
        for row in reader.read_since(since):
            sink.add(row)
            rows += 1
            created_on = _parse_created_on(row.created_on)
            if created_on and (highest_created_on is None or created_on > highest_created_on):
                highest_created_on = created_on
    except PermissionError as exc:
        # A single inaccessible environment must not abort the tenant-wide run.
        logger.warning("%s", exc)
        outcome.rows = rows
        outcome.error = str(exc)
        return outcome
    except (httpx.HTTPError, RuntimeError) as exc:
        logger.exception(
            "Failed to sync environment '%s' (%s).",
            environment.display_name,
            environment.environment_id,
        )
        outcome.rows = rows
        outcome.error = str(exc)
        return outcome

    outcome.rows = rows

    # Only advance the watermark when rows were actually read and ingested.
    # Flushing first means a later ingestion failure cannot silently skip data.
    if highest_created_on is not None:
        sink.flush()
        watermarks.write(
            environment.environment_id, environment.display_name, highest_created_on
        )
    else:
        outcome.skipped = True
        logger.info("No new transcripts in environment '%s'.", environment.display_name)

    return outcome
