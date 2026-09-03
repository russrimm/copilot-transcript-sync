"""Queued ingestion into Azure Data Explorer.

Rows are ingested as newline-delimited JSON against a named ingestion mapping so
that adding columns later does not break existing ingestion.

Duplicates are expected: the watermark replays an overlap window on every run.
Deduplication happens at query time through the ``CopilotTranscript`` materialized
view, which keeps the latest row per ``ConversationTranscriptId``.
"""

from __future__ import annotations

import io
import logging
from typing import Iterable

from azure.core.credentials import TokenCredential
from azure.kusto.data import KustoConnectionStringBuilder
from azure.kusto.data.data_format import DataFormat
from azure.kusto.ingest import IngestionProperties, QueuedIngestClient, ReportLevel

from .dataverse import TranscriptRow

logger = logging.getLogger(__name__)

INGESTION_MAPPING = "CopilotTranscriptRawMapping"

# Keep each ingestion blob comfortably under the 1 GB uncompressed guidance while
# staying large enough to avoid a blob per transcript. A single transcript's
# content column is capped at 1 MB, so 200 rows is a safe upper bound.
DEFAULT_BATCH_ROWS = 200
MAX_BATCH_BYTES = 64 * 1024 * 1024


class AdxTranscriptSink:
    """Buffers transcript rows and flushes them to Azure Data Explorer."""

    def __init__(
        self,
        ingest_uri: str,
        database: str,
        table: str,
        credential: TokenCredential,
        *,
        batch_rows: int = DEFAULT_BATCH_ROWS,
    ) -> None:
        kcsb = KustoConnectionStringBuilder.with_azure_token_credential(
            ingest_uri, credential=credential
        )
        self._client = QueuedIngestClient(kcsb)
        self._properties = IngestionProperties(
            database=database,
            table=table,
            data_format=DataFormat.MULTIJSON,
            ingestion_mapping_reference=INGESTION_MAPPING,
            report_level=ReportLevel.FailuresOnly,
        )
        self._batch_rows = batch_rows
        self._buffer: list[str] = []
        self._buffer_bytes = 0
        self.rows_ingested = 0

    def add(self, row: TranscriptRow) -> None:
        line = row.to_json_line()
        self._buffer.append(line)
        self._buffer_bytes += len(line)
        if len(self._buffer) >= self._batch_rows or self._buffer_bytes >= MAX_BATCH_BYTES:
            self.flush()

    def extend(self, rows: Iterable[TranscriptRow]) -> None:
        for row in rows:
            self.add(row)

    def flush(self) -> None:
        if not self._buffer:
            return

        # MULTIJSON accepts a JSON array of objects.
        payload = "[" + ",".join(self._buffer) + "]"
        stream = io.BytesIO(payload.encode("utf-8"))
        count = len(self._buffer)

        self._client.ingest_from_stream(stream, ingestion_properties=self._properties)

        self.rows_ingested += count
        logger.info("Queued %d transcript rows for ingestion (%d bytes).", count, self._buffer_bytes)
        self._buffer.clear()
        self._buffer_bytes = 0

    def close(self) -> None:
        self.flush()
        self._client.close()

    def __enter__(self) -> "AdxTranscriptSink":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()
