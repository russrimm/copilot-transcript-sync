"""Power Platform environment discovery and application user provisioning.

Uses the legacy Business Application Platform (BAP) admin surface, which is the
only documented endpoint that lists *all* environments in a tenant to an app-only
caller. The newer api.powerplatform.com environment operations are preview and
documented as "for user" only -- Microsoft states plainly that "Power Platform
API uses delegated permissions only at this time", so they are not usable from a
timer-triggered Function without storing a user refresh token.

  Environment listing:
    https://learn.microsoft.com/en-us/power-platform/admin/list-environments
  App-only prerequisite (New-PowerAppManagementApp):
    https://learn.microsoft.com/en-us/power-platform/admin/powershell-create-service-principal
  addAppUser:
    https://learn.microsoft.com/en-us/power-platform/admin/create-dataverseapplicationuser
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Iterator

import httpx

from .credentials import POWERAPPS_SCOPE
from .http import TokenProvider, request_with_retry

logger = logging.getLogger(__name__)

BAP_BASE = "https://api.bap.microsoft.com/providers/Microsoft.BusinessAppPlatform"
BAP_API_VERSION = "2020-10-01"


@dataclass(frozen=True)
class PowerPlatformEnvironment:
    """A discovered environment that has a Dataverse database."""

    environment_id: str
    display_name: str
    environment_url: str
    organization_id: str
    environment_type: str
    state: str
    is_default: bool

    @property
    def api_base(self) -> str:
        return f"{self.environment_url.rstrip('/')}/api/data/v9.2"


def _as_environment(raw: Any) -> PowerPlatformEnvironment | None:
    """Project a BAP environment payload, or None if it has no Dataverse database.

    The BAP documentation publishes only a sample payload, not a field-by-field
    schema, so treat 'no linkedEnvironmentMetadata / no instanceUrl' as
    'no Dataverse database' -- that is an inference, not a documented contract.
    """
    properties = raw.get("properties") or {}
    linked = properties.get("linkedEnvironmentMetadata") or {}
    instance_url = (linked.get("instanceUrl") or "").strip()

    if not instance_url:
        return None

    return PowerPlatformEnvironment(
        environment_id=raw.get("name") or "",
        display_name=properties.get("displayName") or "",
        # instanceUrl is the organization URL and doubles as the token audience.
        environment_url=instance_url.rstrip("/"),
        organization_id=linked.get("resourceId") or "",
        environment_type=properties.get("environmentSku") or "",
        state=linked.get("instanceState") or "",
        is_default=bool(properties.get("isDefault")),
    )


class PowerPlatformAdminClient:
    """Thin client over the BAP admin surface."""

    def __init__(self, client: httpx.Client, tokens: TokenProvider) -> None:
        self._client = client
        self._tokens = tokens

    def _headers(self) -> dict[str, str]:
        headers = self._tokens.auth_header(POWERAPPS_SCOPE)
        headers["Accept"] = "application/json"
        return headers

    def list_environments(self) -> list[PowerPlatformEnvironment]:
        """Return every environment in the tenant that has a Dataverse database."""
        url = f"{BAP_BASE}/scopes/admin/environments?api-version={BAP_API_VERSION}"
        discovered: list[PowerPlatformEnvironment] = []
        total_seen = 0

        for page in self._paged(url):
            for raw in page:
                total_seen += 1
                environment = _as_environment(raw)
                if environment is None:
                    logger.debug(
                        "Skipping environment %s: no Dataverse database.",
                        (raw.get("properties") or {}).get("displayName") or raw.get("name"),
                    )
                    continue
                discovered.append(environment)

        logger.info(
            "Discovered %d environments, %d with a Dataverse database.",
            total_seen,
            len(discovered),
        )
        return discovered

    def _paged(self, url: str) -> Iterator[list[dict[str, Any]]]:
        next_url: str | None = url
        while next_url:
            response = request_with_retry(self._client, "GET", next_url, headers=self._headers())
            if response.status_code == 403:
                raise PermissionError(
                    "BAP admin environment listing returned 403. The app registration must be "
                    "registered as a Power Platform management application first: run "
                    "New-PowerAppManagementApp -ApplicationId <appId> as a tenant admin. "
                    "See scripts/bootstrap-entra.ps1."
                )
            response.raise_for_status()
            payload = response.json()
            yield payload.get("value") or []
            next_url = payload.get("nextLink") or payload.get("@odata.nextLink")

    def ensure_application_user(self, environment_id: str, app_client_id: str) -> bool:
        """Create the Dataverse application user for the app in an environment.

        Returns True when the call succeeded or the user already existed.

        Note the privilege tradeoff: this endpoint always provisions the
        application user as **System Administrator** and offers no way to choose a
        role. The endpoint is also documented as pre-release. See
        scripts/harden-app-user.ps1 to downgrade to a read-only custom role.
        """
        url = (
            f"{BAP_BASE}/scopes/admin/environments/{environment_id}"
            f"/addAppUser?api-version={BAP_API_VERSION}"
        )
        response = request_with_retry(
            self._client,
            "POST",
            url,
            headers=self._headers(),
            json_body={"servicePrincipalAppId": app_client_id},
        )

        if response.is_success:
            logger.info("Ensured application user in environment %s.", environment_id)
            return True

        # Dataverse permits only one application user per registered app per
        # environment, so a conflict means the desired state already holds.
        if response.status_code == 409:
            logger.debug("Application user already present in environment %s.", environment_id)
            return True

        logger.warning(
            "Could not provision application user in environment %s: HTTP %d %s",
            environment_id,
            response.status_code,
            response.text[:500],
        )
        return False


def is_transcript_eligible(
    environment: PowerPlatformEnvironment, excluded_types: frozenset[str]
) -> bool:
    """Whether an environment should be queried for transcripts.

    No environment type is excluded by default, deliberately.

    The costs are asymmetric: querying an environment that holds no transcripts
    costs one request returning zero rows, while skipping one that does hold
    transcripts loses that data permanently once the 30-day Dataverse retention
    passes. Measured in the tenant this was built against, Developer
    environments held more transcripts than Production environments did.

    Use EXCLUDED_ENVIRONMENT_TYPES to opt out of a type once you have confirmed
    it holds nothing in your own tenant. scripts/probe_all_environments.py
    reports the actual counts.
    """
    environment_type = environment.environment_type.casefold()
    if environment_type in excluded_types:
        logger.info(
            "Skipping environment '%s' (%s): type is in EXCLUDED_ENVIRONMENT_TYPES.",
            environment.display_name,
            environment.environment_type or "unknown",
        )
        return False

    if environment.state and environment.state.casefold() != "ready":
        logger.info(
            "Skipping environment '%s': Dataverse instance state is '%s'.",
            environment.display_name,
            environment.state,
        )
        return False

    return True
