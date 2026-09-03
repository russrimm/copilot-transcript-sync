"""Extraction of ``conversationtranscript`` rows from a Dataverse environment.

Incremental strategy: a ``createdon`` high-water mark per environment, replayed
with a configurable overlap, deduplicated downstream in Azure Data Explorer.

Dataverse change tracking (``Prefer: odata.track-changes``) was deliberately not
used. Three reasons:

* It propagates deletes. Dataverse bulk-deletes transcripts after 30 days by
  default, so a delete-aware sync would erase the archive this job exists to
  build.
* It forbids ``$filter``, ``$orderby``, ``$top`` and ``$expand`` on the same
  request, which removes any ability to bound a run.
* Delta tokens expire after a default of seven days, so an outage longer than
  that silently forces a reseed anyway.

  https://learn.microsoft.com/en-us/power-apps/developer/data-platform/use-change-tracking-synchronize-data-external-systems
  https://learn.microsoft.com/en-us/microsoft-copilot-studio/analytics-transcripts-powerapps
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterator

import httpx

from .credentials import dataverse_scope
from .http import TokenProvider, request_with_retry
from .powerplatform import PowerPlatformEnvironment

logger = logging.getLogger(__name__)

TRANSCRIPT_COLUMNS = (
    "conversationtranscriptid",
    "name",
    "content",
    "metadata",
    "conversationstarttime",
    "createdon",
    "modifiedon",
    "versionnumber",
    "schematype",
    "schemaversion",
    "_bot_conversationtranscriptid_value",
)


@dataclass(frozen=True)
class TranscriptRow:
    """A single ``conversationtranscript`` row, flattened for ingestion."""

    environment_id: str
    environment_name: str
    environment_url: str
    organization_id: str
    conversation_transcript_id: str
    name: str
    conversation_id: str
    bot_id: str
    bot_name: str
    batch_id: str
    aad_tenant_id: str
    schema_type: str
    schema_version: str
    conversation_start_time: str | None
    created_on: str | None
    modified_on: str | None
    version_number: int | None
    metadata: Any
    content: Any
    content_parse_error: str | None
    ingested_at: str

    def to_json_line(self) -> str:
        return json.dumps(
            {
                "EnvironmentId": self.environment_id,
                "EnvironmentName": self.environment_name,
                "EnvironmentUrl": self.environment_url,
                "OrganizationId": self.organization_id,
                "ConversationTranscriptId": self.conversation_transcript_id,
                "Name": self.name,
                "ConversationId": self.conversation_id,
                "BotId": self.bot_id,
                "BotName": self.bot_name,
                "BatchId": self.batch_id,
                "AadTenantId": self.aad_tenant_id,
                "SchemaType": self.schema_type,
                "SchemaVersion": self.schema_version,
                "ConversationStartTime": self.conversation_start_time,
                "CreatedOn": self.created_on,
                "ModifiedOn": self.modified_on,
                "VersionNumber": self.version_number,
                "Metadata": self.metadata,
                "Content": self.content,
                "ContentParseError": self.content_parse_error,
                "IngestedAt": self.ingested_at,
            },
            separators=(",", ":"),
            ensure_ascii=False,
        )


def _loads(raw: Any) -> tuple[Any, str | None]:
    """Parse an embedded JSON string, returning (value, error)."""
    if raw is None or raw == "":
        return None, None
    if not isinstance(raw, str):
        return raw, None
    try:
        return json.loads(raw), None
    except (ValueError, TypeError) as exc:
        return None, str(exc)


def _conversation_id(name: str, bot_id: str) -> str:
    """Recover the conversation ID from ``Name``.

    ``Name`` is documented as ConversationId + "_" + BotId. A conversation ID can
    itself contain underscores, so trim the known BotId suffix rather than
    splitting on the separator.
    """
    if not name:
        return ""
    suffix = f"_{bot_id}"
    if bot_id and name.endswith(suffix):
        return name[: -len(suffix)]
    head, _, _tail = name.rpartition("_")
    return head or name


def _odata_datetime(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class DataverseTranscriptReader:
    """Reads transcripts from one environment."""

    def __init__(
        self,
        client: httpx.Client,
        tokens: TokenProvider,
        environment: PowerPlatformEnvironment,
        page_size: int,
    ) -> None:
        self._client = client
        self._tokens = tokens
        self._environment = environment
        self._page_size = page_size

    def _headers(self) -> dict[str, str]:
        headers = self._tokens.auth_header(dataverse_scope(self._environment.environment_url))
        headers.update(
            {
                "Accept": "application/json",
                "OData-MaxVersion": "4.0",
                "OData-Version": "4.0",
                # Bound the response size: a single transcript can carry 1 MB of
                # content, so a large page can mean a very large payload.
                "Prefer": f"odata.maxpagesize={self._page_size}",
            }
        )
        return headers

    def _initial_url(self, since: datetime) -> str:
        select = ",".join(TRANSCRIPT_COLUMNS)
        return (
            f"{self._environment.api_base}/conversationtranscripts"
            f"?$select={select}"
            f"&$filter=createdon gt {_odata_datetime(since)}"
            f"&$orderby=createdon asc"
        )

    def read_since(self, since: datetime) -> Iterator[TranscriptRow]:
        """Yield transcripts created after ``since``, oldest first."""
        ingested_at = datetime.now(timezone.utc).isoformat()
        next_url: str | None = self._initial_url(since)
        page_index = 0

        while next_url:
            response = request_with_retry(self._client, "GET", next_url, headers=self._headers())

            if response.status_code in (401, 403):
                raise PermissionError(
                    f"Dataverse returned HTTP {response.status_code} for environment "
                    f"'{self._environment.display_name}' ({self._environment.environment_url}). "
                    "The application user is probably missing or lacks Read on "
                    "conversationtranscript."
                )
            response.raise_for_status()

            payload = response.json()
            rows = payload.get("value") or []
            page_index += 1
            logger.debug(
                "Environment '%s' page %d returned %d transcripts.",
                self._environment.display_name,
                page_index,
                len(rows),
            )

            for raw in rows:
                yield self._project(raw, ingested_at)

            next_url = payload.get("@odata.nextLink")

    def _project(self, raw: dict[str, Any], ingested_at: str) -> TranscriptRow:
        metadata, _metadata_error = _loads(raw.get("metadata"))
        content, content_error = _loads(raw.get("content"))

        metadata_map = metadata if isinstance(metadata, dict) else {}
        bot_id = str(
            metadata_map.get("BotId") or raw.get("_bot_conversationtranscriptid_value") or ""
        )
        batch_id = metadata_map.get("BatchId")

        name = raw.get("name") or ""

        return TranscriptRow(
            environment_id=self._environment.environment_id,
            environment_name=self._environment.display_name,
            environment_url=self._environment.environment_url,
            organization_id=self._environment.organization_id,
            conversation_transcript_id=raw.get("conversationtranscriptid") or "",
            name=name,
            conversation_id=_conversation_id(name, bot_id),
            bot_id=bot_id,
            bot_name=str(metadata_map.get("BotName") or ""),
            batch_id="" if batch_id is None else str(batch_id),
            aad_tenant_id=str(metadata_map.get("AADTenantId") or ""),
            schema_type=raw.get("schematype") or "",
            schema_version=raw.get("schemaversion") or "",
            conversation_start_time=raw.get("conversationstarttime"),
            created_on=raw.get("createdon"),
            modified_on=raw.get("modifiedon"),
            version_number=_safe_int(raw.get("versionnumber")),
            metadata=metadata,
            content=content,
            content_parse_error=content_error,
            ingested_at=ingested_at,
        )


def _safe_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
