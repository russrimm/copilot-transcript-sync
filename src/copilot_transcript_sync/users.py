"""Resolution of Entra object IDs to organizational attributes, via Microsoft Graph.

Transcript activities carry two user identifiers, and only one is useful here:

* ``from.id`` is hashed before being written to the transcript, so it identifies
  a user consistently within a conversation but cannot be resolved to a person.
* ``from.aadObjectId`` is the real Entra object ID, and is present on user
  activities for authenticated channels.

Resolving the second against Microsoft Graph is what makes departmental and
role-based segmentation possible -- comparing agent adoption across departments,
or finding which job functions escalate most.

Authentication uses the Function's managed identity directly, holding the
``User.Read.All`` application permission on Microsoft Graph. That is separate
from the Power Platform identity: Graph is an Azure resource, so no federation
hop is involved.

**This resolves personal data.** Names, job titles, and managers are ingested
into the analytics store, so treat that store accordingly and set
``SYNC_USERS=false`` if your privacy posture does not allow it.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Iterator

import httpx
from azure.core.credentials import TokenCredential

from .http import TokenProvider, request_with_retry

logger = logging.getLogger(__name__)

GRAPH_SCOPE = "https://graph.microsoft.com/.default"
GRAPH_BASE = "https://graph.microsoft.com/v1.0"

# Graph caps $batch at 20 requests, and getByIds at 1000 ids.
# https://learn.microsoft.com/en-us/graph/json-batching
GET_BY_IDS_LIMIT = 1000

USER_FIELDS = (
    "id",
    "displayName",
    "userPrincipalName",
    "mail",
    "jobTitle",
    "department",
    "companyName",
    "officeLocation",
    "city",
    "country",
    "usageLocation",
    "accountEnabled",
    "employeeType",
)


@dataclass(frozen=True)
class UserRow:
    """A resolved Entra user, flattened for ingestion."""

    aad_object_id: str
    display_name: str
    user_principal_name: str
    mail: str
    job_title: str
    department: str
    company_name: str
    office_location: str
    city: str
    country: str
    usage_location: str
    employee_type: str
    account_enabled: bool | None
    resolved: bool
    ingested_at: str

    def to_json_line(self) -> str:
        return json.dumps(
            {
                "AadObjectId": self.aad_object_id,
                "DisplayName": self.display_name,
                "UserPrincipalName": self.user_principal_name,
                "Mail": self.mail,
                "JobTitle": self.job_title,
                "Department": self.department,
                "CompanyName": self.company_name,
                "OfficeLocation": self.office_location,
                "City": self.city,
                "Country": self.country,
                "UsageLocation": self.usage_location,
                "EmployeeType": self.employee_type,
                "AccountEnabled": self.account_enabled,
                "Resolved": self.resolved,
                "IngestedAt": self.ingested_at,
            },
            separators=(",", ":"),
            ensure_ascii=False,
        )


def _chunks(items: list[str], size: int) -> Iterator[list[str]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


class GraphUserResolver:
    """Resolves Entra object IDs to organizational attributes."""

    def __init__(self, client: httpx.Client, credential: TokenCredential) -> None:
        self._client = client
        self._tokens = TokenProvider(credential)

    def _headers(self) -> dict[str, str]:
        headers = self._tokens.auth_header(GRAPH_SCOPE)
        headers.update({"Accept": "application/json", "Content-Type": "application/json"})
        return headers

    def resolve(self, object_ids: Iterable[str]) -> Iterator[UserRow]:
        """Yield one row per requested object ID.

        Unresolvable IDs still produce a row with ``Resolved = false``, so a
        deleted or external user does not silently vanish from the dimension and
        leave sessions unattributable.
        """
        unique = sorted({oid.strip() for oid in object_ids if oid and oid.strip()})
        if not unique:
            return

        ingested_at = datetime.now(timezone.utc).isoformat()
        logger.info("Resolving %d distinct Entra object IDs via Microsoft Graph.", len(unique))

        for batch in _chunks(unique, GET_BY_IDS_LIMIT):
            found: dict[str, dict[str, Any]] = {}

            response = request_with_retry(
                self._client,
                "POST",
                f"{GRAPH_BASE}/directoryObjects/getByIds",
                headers=self._headers(),
                json_body={"ids": batch, "types": ["user"]},
            )

            if response.status_code == 403:
                raise PermissionError(
                    "Microsoft Graph returned 403 resolving users. The managed identity "
                    "needs the User.Read.All application permission. See "
                    "scripts/grant_graph_permission.ps1."
                )
            if response.is_success:
                for item in response.json().get("value") or []:
                    if item.get("id"):
                        found[item["id"]] = item
            else:
                logger.warning(
                    "Graph getByIds failed with HTTP %d: %s",
                    response.status_code,
                    response.text[:300],
                )

            for object_id in batch:
                yield _project(object_id, found.get(object_id), ingested_at)


def _project(object_id: str, user: dict[str, Any] | None, ingested_at: str) -> UserRow:
    user = user or {}
    return UserRow(
        aad_object_id=object_id,
        display_name=str(user.get("displayName") or ""),
        user_principal_name=str(user.get("userPrincipalName") or ""),
        mail=str(user.get("mail") or ""),
        job_title=str(user.get("jobTitle") or ""),
        department=str(user.get("department") or ""),
        company_name=str(user.get("companyName") or ""),
        office_location=str(user.get("officeLocation") or ""),
        city=str(user.get("city") or ""),
        country=str(user.get("country") or ""),
        usage_location=str(user.get("usageLocation") or ""),
        employee_type=str(user.get("employeeType") or ""),
        account_enabled=user.get("accountEnabled"),
        resolved=bool(user),
        ingested_at=ingested_at,
    )
