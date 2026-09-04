"""Configuration resolved from application settings (environment variables)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

_TRUE = {"1", "true", "yes", "on"}


def _get(name: str, default: str | None = None, *, required: bool = False) -> str:
    value = os.environ.get(name, default if default is not None else "")
    if required and not value:
        raise RuntimeError(
            f"Required application setting '{name}' is missing. "
            "See local.settings.json.example for the full list."
        )
    return value


def _get_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise RuntimeError(f"Application setting '{name}' must be an integer, got {raw!r}.") from exc


def _get_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in _TRUE


@dataclass(frozen=True)
class Settings:
    """Runtime configuration for the sync job."""

    tenant_id: str
    app_client_id: str
    uami_client_id: str

    adx_cluster_uri: str
    adx_ingest_uri: str
    adx_database: str
    adx_raw_table: str

    watermark_table_endpoint: str
    watermark_table_name: str

    initial_backfill_days: int
    watermark_lookback_minutes: int
    dataverse_page_size: int
    max_concurrent_environments: int

    auto_provision_app_user: bool
    read_only_role_name: str
    excluded_environment_types: frozenset[str] = field(default_factory=frozenset)

    @classmethod
    def from_environment(cls) -> "Settings":
        # Empty by default. Microsoft documents that Developer and Teams
        # environments never persist transcripts, but that is demonstrably wrong
        # for Developer, so nothing is excluded unless you opt in.
        excluded = {
            part.strip().casefold()
            for part in _get("EXCLUDED_ENVIRONMENT_TYPES", "").split(",")
            if part.strip()
        }
        return cls(
            tenant_id=_get("PP_TENANT_ID", required=True),
            app_client_id=_get("PP_APP_CLIENT_ID", required=True),
            uami_client_id=_get("UAMI_CLIENT_ID"),
            adx_cluster_uri=_get("ADX_CLUSTER_URI", required=True).rstrip("/"),
            adx_ingest_uri=_get("ADX_INGEST_URI", required=True).rstrip("/"),
            adx_database=_get("ADX_DATABASE", required=True),
            adx_raw_table=_get("ADX_RAW_TABLE", "CopilotTranscriptRaw"),
            watermark_table_endpoint=_get("WATERMARK_TABLE_ENDPOINT", required=True).rstrip("/"),
            watermark_table_name=_get("WATERMARK_TABLE_NAME", "SyncWatermarks"),
            initial_backfill_days=_get_int("INITIAL_BACKFILL_DAYS", 30),
            watermark_lookback_minutes=_get_int("WATERMARK_LOOKBACK_MINUTES", 120),
            dataverse_page_size=_get_int("DATAVERSE_PAGE_SIZE", 25),
            max_concurrent_environments=_get_int("MAX_CONCURRENT_ENVIRONMENTS", 4),
            auto_provision_app_user=_get_bool("AUTO_PROVISION_APP_USER", True),
            read_only_role_name=_get("READ_ONLY_ROLE_NAME"),
            excluded_environment_types=frozenset(excluded),
        )
