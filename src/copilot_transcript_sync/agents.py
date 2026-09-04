"""Extraction of the ``bot`` table -- Copilot Studio agent metadata.

Transcripts carry only ``BotId`` and ``BotName``. The ``bot`` table adds the
governance and configuration metadata that makes agent reporting useful:
authentication mode, access control policy, publish state, owner, language, and
a configuration blob describing generative settings.

Column names here were read from a live environment's ``EntityDefinitions``
rather than from documentation. Run ``scripts/probe_table_schema.py --table bot``
to re-check them against your own tenant.

Agents are a small, slowly changing dimension, so the whole table is read on
every run rather than watermarked. A tenant with thousands of agents would still
be a handful of requests.
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

AGENT_COLUMNS = (
    "botid",
    "name",
    "schemaname",
    "language",
    "authenticationmode",
    "authenticationtrigger",
    "accesscontrolpolicy",
    "statecode",
    "statuscode",
    "template",
    "componentstate",
    "ismanaged",
    "solutionid",
    "publishedon",
    "createdon",
    "modifiedon",
    "versionnumber",
    "configuration",
    "_ownerid_value",
    "_owninguser_value",
    "_owningbusinessunit_value",
    "_publishedby_value",
    "_createdby_value",
    "_modifiedby_value",
)

# Picklist labels, from the bot table's global choice sets. Kept here so the
# archive is readable without a separate metadata lookup per environment.
# https://learn.microsoft.com/en-us/power-apps/developer/data-platform/reference/entities/bot
ACCESS_CONTROL_POLICY = {
    0: "Any",
    1: "Copilot readers",
    2: "Group membership",
    3: "Any (multi-tenant)",
}

STATE_CODE = {0: "Active", 1: "Inactive"}


@dataclass(frozen=True)
class AgentRow:
    """A single ``bot`` row, flattened for ingestion."""

    environment_id: str
    environment_name: str
    environment_url: str
    bot_id: str
    name: str
    schema_name: str
    language: int | None
    authentication_mode: int | None
    authentication_trigger: int | None
    access_control_policy: int | None
    access_control_policy_label: str
    state_code: int | None
    state_label: str
    status_code: int | None
    template: str
    component_state: int | None
    is_managed: bool | None
    solution_id: str
    published_on: str | None
    created_on: str | None
    modified_on: str | None
    version_number: int | None
    owner_id: str
    owning_user_id: str
    owning_business_unit_id: str
    published_by_id: str
    created_by_id: str
    modified_by_id: str
    configuration: Any
    ingested_at: str

    def to_json_line(self) -> str:
        return json.dumps(
            {
                "EnvironmentId": self.environment_id,
                "EnvironmentName": self.environment_name,
                "EnvironmentUrl": self.environment_url,
                "BotId": self.bot_id,
                "Name": self.name,
                "SchemaName": self.schema_name,
                "Language": self.language,
                "AuthenticationMode": self.authentication_mode,
                "AuthenticationTrigger": self.authentication_trigger,
                "AccessControlPolicy": self.access_control_policy,
                "AccessControlPolicyLabel": self.access_control_policy_label,
                "StateCode": self.state_code,
                "StateLabel": self.state_label,
                "StatusCode": self.status_code,
                "Template": self.template,
                "ComponentState": self.component_state,
                "IsManaged": self.is_managed,
                "SolutionId": self.solution_id,
                "PublishedOn": self.published_on,
                "CreatedOn": self.created_on,
                "ModifiedOn": self.modified_on,
                "VersionNumber": self.version_number,
                "OwnerId": self.owner_id,
                "OwningUserId": self.owning_user_id,
                "OwningBusinessUnitId": self.owning_business_unit_id,
                "PublishedById": self.published_by_id,
                "CreatedById": self.created_by_id,
                "ModifiedById": self.modified_by_id,
                "Configuration": self.configuration,
                "IngestedAt": self.ingested_at,
            },
            separators=(",", ":"),
            ensure_ascii=False,
        )


def _int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _loads(raw: Any) -> Any:
    """Parse the configuration blob, returning None rather than raising."""
    if raw is None or raw == "":
        return None
    if not isinstance(raw, str):
        return raw
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        # Configuration is documented only as a string; keep the raw text rather
        # than losing it.
        return {"raw": raw[:4000]}


class DataverseAgentReader:
    """Reads the ``bot`` table from one environment."""

    def __init__(
        self,
        client: httpx.Client,
        tokens: TokenProvider,
        environment: PowerPlatformEnvironment,
        page_size: int = 100,
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
                "Prefer": f"odata.maxpagesize={self._page_size}",
            }
        )
        return headers

    def read_all(self) -> Iterator[AgentRow]:
        """Yield every agent in the environment."""
        ingested_at = datetime.now(timezone.utc).isoformat()
        select = ",".join(AGENT_COLUMNS)
        next_url: str | None = f"{self._environment.api_base}/bots?$select={select}"

        while next_url:
            response = request_with_retry(self._client, "GET", next_url, headers=self._headers())

            if response.status_code in (401, 403):
                raise PermissionError(
                    f"Dataverse returned HTTP {response.status_code} reading the bot table in "
                    f"'{self._environment.display_name}'. The application user needs Read on bot."
                )
            if response.status_code == 404:
                logger.info(
                    "No bot table in environment '%s'; skipping agent sync.",
                    self._environment.display_name,
                )
                return
            response.raise_for_status()

            payload = response.json()
            for raw in payload.get("value") or []:
                yield self._project(raw, ingested_at)

            next_url = payload.get("@odata.nextLink")

    def _project(self, raw: dict[str, Any], ingested_at: str) -> AgentRow:
        policy = _int(raw.get("accesscontrolpolicy"))
        state = _int(raw.get("statecode"))

        return AgentRow(
            environment_id=self._environment.environment_id,
            environment_name=self._environment.display_name,
            environment_url=self._environment.environment_url,
            bot_id=raw.get("botid") or "",
            name=raw.get("name") or "",
            schema_name=raw.get("schemaname") or "",
            language=_int(raw.get("language")),
            authentication_mode=_int(raw.get("authenticationmode")),
            authentication_trigger=_int(raw.get("authenticationtrigger")),
            access_control_policy=policy,
            access_control_policy_label=ACCESS_CONTROL_POLICY.get(policy, ""),
            state_code=state,
            state_label=STATE_CODE.get(state, ""),
            status_code=_int(raw.get("statuscode")),
            template=raw.get("template") or "",
            component_state=_int(raw.get("componentstate")),
            is_managed=raw.get("ismanaged"),
            solution_id=raw.get("solutionid") or "",
            published_on=raw.get("publishedon"),
            created_on=raw.get("createdon"),
            modified_on=raw.get("modifiedon"),
            version_number=_int(raw.get("versionnumber")),
            owner_id=raw.get("_ownerid_value") or "",
            owning_user_id=raw.get("_owninguser_value") or "",
            owning_business_unit_id=raw.get("_owningbusinessunit_value") or "",
            published_by_id=raw.get("_publishedby_value") or "",
            created_by_id=raw.get("_createdby_value") or "",
            modified_by_id=raw.get("_modifiedby_value") or "",
            configuration=_loads(raw.get("configuration")),
            ingested_at=ingested_at,
        )
