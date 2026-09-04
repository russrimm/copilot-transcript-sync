"""Orchestration: discover environments, extract transcripts, ingest into ADX."""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

import httpx

from .adx import AdxSink, agent_sink, transcript_sink, user_sink
from .agents import DataverseAgentReader
from .credentials import azure_credential, power_platform_credential
from .dataverse import DataverseTranscriptReader, extract_aad_object_ids
from .http import DEFAULT_TIMEOUT, TokenProvider
from .powerplatform import (
    PowerPlatformAdminClient,
    PowerPlatformEnvironment,
    is_transcript_eligible,
)
from .settings import Settings
from .users import GraphUserResolver
from .watermarks import WatermarkStore

logger = logging.getLogger(__name__)


@dataclass
class EnvironmentResult:
    environment_id: str
    environment_name: str
    environment_type: str
    rows: int = 0
    agents: int = 0
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
    agents_ingested: int = 0
    users_resolved: int = 0
    users_error: str | None = None
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

    # Each consumer gets its own Azure credential. The Kusto SDK closes the
    # credential it was handed when its client closes, so a shared instance
    # would leave later consumers with a closed transport.
    watermarks = WatermarkStore(
        endpoint=settings.watermark_table_endpoint,
        table_name=settings.watermark_table_name,
        credential=azure_credential(settings),
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

            with transcript_sink(
                settings.adx_ingest_uri,
                settings.adx_database,
                settings.adx_raw_table,
                azure_credential(settings),
            ) as sink, _maybe_agent_sink(settings) as agents:
                # Object IDs seen across the whole run, resolved once at the end
                # rather than per environment, since the same person can appear
                # in several environments.
                seen_object_ids: set[str] = set()

                for environment in eligible:
                    outcome = _sync_environment(
                        environment=environment,
                        settings=settings,
                        admin=admin,
                        http_client=http_client,
                        pp_tokens=pp_tokens,
                        sink=sink,
                        agent_sink_=agents,
                        watermarks=watermarks,
                        seen_object_ids=seen_object_ids,
                    )
                    result.environments.append(outcome)
                    if outcome.error:
                        result.environments_failed += 1
                    elif not outcome.skipped:
                        result.environments_synced += 1

                # Flush before reading the totals so counts reflect real work.
                sink.flush()
                result.rows_ingested = sink.rows_ingested
                if agents is not None:
                    agents.flush()
                    result.agents_ingested = agents.rows_ingested

            if settings.sync_users and seen_object_ids:
                result.users_resolved, result.users_error = _sync_users(
                    settings, http_client, seen_object_ids
                )
    finally:
        watermarks.close()

    result.finished_at = datetime.now(timezone.utc).isoformat()
    logger.info(
        "Sync complete: %d/%d environments synced, %d failed, %d transcripts, "
        "%d agents, %d users.",
        result.environments_synced,
        result.environments_discovered,
        result.environments_failed,
        result.rows_ingested,
        result.agents_ingested,
        result.users_resolved,
    )
    return result


def _sync_users(
    settings: Settings,
    http_client: httpx.Client,
    object_ids: set[str],
) -> tuple[int, str | None]:
    """Resolve Entra object IDs to org attributes and ingest them.

    Returns (count, error). Failure is not fatal -- the user dimension is an
    enrichment, and losing it must not cost transcripts already collected -- but
    the reason is returned rather than swallowed, because a silent zero looks
    identical to "no users found".
    """
    try:
        resolver = GraphUserResolver(http_client, azure_credential(settings))
        with user_sink(
            settings.adx_ingest_uri,
            settings.adx_database,
            settings.adx_user_table,
            azure_credential(settings),
        ) as users:
            for user in resolver.resolve(object_ids):
                users.add(user)
            users.flush()
            return users.rows_ingested, None
    except Exception as exc:  # noqa: BLE001 - enrichment must not break the run
        message = f"{type(exc).__name__}: {str(exc)[:300]}"
        logger.warning("Could not resolve Entra users: %s", message)
        return 0, message


class _NullSink:
    """Stands in for the agent sink when agent sync is disabled."""

    rows_ingested = 0

    def add(self, _row: object) -> None:  # pragma: no cover - trivial
        raise RuntimeError("Agent sync is disabled.")

    def flush(self) -> None:
        return

    def close(self) -> None:
        return

    def __enter__(self):
        return None

    def __exit__(self, *_exc: object) -> None:
        return


def _maybe_agent_sink(settings: Settings):
    """Return an agent sink, or a no-op context yielding None when disabled."""
    if not settings.sync_agents:
        return _NullSink()
    return agent_sink(
        settings.adx_ingest_uri,
        settings.adx_database,
        settings.adx_agent_table,
        azure_credential(settings),
    )


def _sync_environment(
    *,
    environment: PowerPlatformEnvironment,
    settings: Settings,
    admin: PowerPlatformAdminClient,
    http_client: httpx.Client,
    pp_tokens: TokenProvider,
    sink: AdxSink,
    agent_sink_: AdxSink | None,
    watermarks: WatermarkStore,
    seen_object_ids: set[str],
) -> EnvironmentResult:
    outcome = EnvironmentResult(
        environment_id=environment.environment_id,
        environment_name=environment.display_name,
        environment_type=environment.environment_type,
    )

    if settings.auto_provision_app_user:
        admin.ensure_application_user(environment.environment_id, settings.app_client_id)

    # Agents are a small dimension, read in full each run. A failure here must
    # not stop transcript extraction, which is the point of the pipeline.
    if agent_sink_ is not None:
        try:
            agent_reader = DataverseAgentReader(http_client, pp_tokens, environment)
            for agent in agent_reader.read_all():
                agent_sink_.add(agent)
                outcome.agents += 1
        except (PermissionError, httpx.HTTPError, RuntimeError) as exc:
            logger.warning(
                "Could not read agents in '%s': %s", environment.display_name, str(exc)[:200]
            )

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
            seen_object_ids.update(extract_aad_object_ids(row.content))
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
        # Agents may still have been collected, so "skipped" means only that no
        # new transcripts arrived.
        outcome.skipped = True
        logger.info(
            "No new transcripts in environment '%s' (%d agents read).",
            environment.display_name,
            outcome.agents,
        )

    return outcome
