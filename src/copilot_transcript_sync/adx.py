"""Queued ingestion into Azure Data Explorer.

Rows are ingested as JSON against a named ingestion mapping so that adding
columns later does not break existing ingestion.

Duplicates are expected: the watermark replays an overlap window on every run.
Deduplication happens at query time through the materialized views, which keep
the latest row per business key.
"""

from __future__ import annotations

import io
import logging
from typing import Iterable, Protocol

from azure.core.credentials import TokenCredential
from azure.kusto.data import KustoConnectionStringBuilder
from azure.kusto.data.data_format import DataFormat
from azure.kusto.data.exceptions import KustoNetworkError
from azure.kusto.ingest import IngestionProperties, QueuedIngestClient, ReportLevel

logger = logging.getLogger(__name__)

TRANSCRIPT_MAPPING = "CopilotTranscriptRawMapping"
AGENT_MAPPING = "CopilotAgentRawMapping"
USER_MAPPING = "CopilotUserRawMapping"

# Keep each ingestion blob comfortably under the 1 GB uncompressed guidance while
# staying large enough to avoid a blob per row. A single transcript's content
# column is capped at 1 MB, so 200 rows is a safe upper bound.
DEFAULT_BATCH_ROWS = 200
MAX_BATCH_BYTES = 64 * 1024 * 1024


class IngestableRow(Protocol):
    """Anything that can serialize itself to a single-line JSON object."""

    def to_json_line(self) -> str: ...


class AdxSink:
    """Buffers rows and flushes them to one Azure Data Explorer table."""

    def __init__(
        self,
        ingest_uri: str,
        database: str,
        table: str,
        mapping: str,
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
            ingestion_mapping_reference=mapping,
            report_level=ReportLevel.FailuresOnly,
        )
        self._table = table
        self._ingest_uri = ingest_uri
        self._batch_rows = batch_rows
        self._buffer: list[str] = []
        self._buffer_bytes = 0
        self.rows_ingested = 0

    def add(self, row: IngestableRow) -> None:
        line = row.to_json_line()
        self._buffer.append(line)
        self._buffer_bytes += len(line)
        if len(self._buffer) >= self._batch_rows or self._buffer_bytes >= MAX_BATCH_BYTES:
            self.flush()

    def extend(self, rows: Iterable[IngestableRow]) -> None:
        for row in rows:
            self.add(row)

    def flush(self) -> None:
        if not self._buffer:
            return

        # MULTIJSON accepts a JSON array of objects.
        payload = "[" + ",".join(self._buffer) + "]"
        stream = io.BytesIO(payload.encode("utf-8"))
        count = len(self._buffer)

        try:
            self._client.ingest_from_stream(stream, ingestion_properties=self._properties)
        except KustoNetworkError as exc:
            # A stopped Azure Data Explorer cluster surfaces here as a bare
            # network failure against the auth metadata endpoint, naming nothing
            # about the cluster state. Say so, because the raw error sends you
            # looking at networking instead.
            raise RuntimeError(
                f"Could not reach Azure Data Explorer at {self._ingest_uri}. "
                "The usual cause is that the cluster is stopped: a stopped cluster "
                "does not restart itself, and every request fails as a network error. "
                "Check the cluster state and start it if needed:\n"
                "  az resource show -g <rg> -n <cluster> "
                "--resource-type Microsoft.Kusto/clusters "
                "--api-version 2024-04-13 --query properties.state\n"
                f"Underlying error: {exc}"
            ) from exc

        self.rows_ingested += count
        logger.info(
            "Queued %d rows for ingestion into %s (%d bytes).",
            count,
            self._table,
            self._buffer_bytes,
        )
        self._buffer.clear()
        self._buffer_bytes = 0

    def close(self) -> None:
        self.flush()
        self._client.close()

    def __enter__(self) -> "AdxSink":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def transcript_sink(
    ingest_uri: str, database: str, table: str, credential: TokenCredential
) -> AdxSink:
    return AdxSink(ingest_uri, database, table, TRANSCRIPT_MAPPING, credential)


def agent_sink(
    ingest_uri: str, database: str, table: str, credential: TokenCredential
) -> AdxSink:
    # Agent rows are small, so a larger batch keeps extent counts sensible.
    return AdxSink(ingest_uri, database, table, AGENT_MAPPING, credential, batch_rows=500)


def user_sink(
    ingest_uri: str, database: str, table: str, credential: TokenCredential
) -> AdxSink:
    return AdxSink(ingest_uri, database, table, USER_MAPPING, credential, batch_rows=500)


# Backwards-compatible alias for the original transcript-only sink.
AdxTranscriptSink = AdxSink

