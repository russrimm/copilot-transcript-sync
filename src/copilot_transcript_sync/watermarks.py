"""Per-environment high-water marks, stored in Azure Table Storage.

One entity per environment. The stored value is the maximum ``createdon`` seen,
and reads replay a configurable overlap window before it, because transcripts are
written to Dataverse only after 30 minutes of conversation inactivity and can
therefore land slightly out of order relative to a strictly advancing watermark.
Overlap produces duplicates by design; Azure Data Explorer deduplicates them.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from azure.core.credentials import TokenCredential
from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError
from azure.data.tables import TableClient

logger = logging.getLogger(__name__)

PARTITION_KEY = "environment"
_WATERMARK_FIELD = "LastCreatedOn"


def _parse(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


class WatermarkStore:
    """Reads and writes the per-environment sync watermark."""

    def __init__(
        self,
        endpoint: str,
        table_name: str,
        credential: TokenCredential,
        *,
        initial_backfill_days: int,
        lookback_minutes: int,
    ) -> None:
        self._client = TableClient(
            endpoint=endpoint, table_name=table_name, credential=credential
        )
        self._initial_backfill_days = initial_backfill_days
        self._lookback_minutes = lookback_minutes
        self._ensure_table()

    def _ensure_table(self) -> None:
        try:
            self._client.create_table()
            logger.info("Created watermark table '%s'.", self._client.table_name)
        except ResourceExistsError:
            pass

    def read_start_time(self, environment_id: str) -> datetime:
        """Return the timestamp to resume from, including the overlap window."""
        try:
            entity = self._client.get_entity(PARTITION_KEY, environment_id)
        except ResourceNotFoundError:
            start = datetime.now(timezone.utc) - timedelta(days=self._initial_backfill_days)
            logger.info(
                "No watermark for environment %s; backfilling from %s.",
                environment_id,
                start.isoformat(),
            )
            return start

        stored = _parse(str(entity.get(_WATERMARK_FIELD, "")))
        if stored is None:
            start = datetime.now(timezone.utc) - timedelta(days=self._initial_backfill_days)
            logger.warning(
                "Unreadable watermark for environment %s; backfilling from %s.",
                environment_id,
                start.isoformat(),
            )
            return start

        return stored - timedelta(minutes=self._lookback_minutes)

    def write(self, environment_id: str, environment_name: str, watermark: datetime) -> None:
        self._client.upsert_entity(
            {
                "PartitionKey": PARTITION_KEY,
                "RowKey": environment_id,
                "EnvironmentName": environment_name,
                _WATERMARK_FIELD: watermark.astimezone(timezone.utc).isoformat(),
                "UpdatedAt": datetime.now(timezone.utc).isoformat(),
            }
        )
        logger.info(
            "Watermark for '%s' advanced to %s.", environment_name, watermark.isoformat()
        )

    def close(self) -> None:
        self._client.close()
