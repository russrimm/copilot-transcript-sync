"""Credential construction.

Two distinct identities are in play, and conflating them is the easiest way to
break this job:

1. The *user-assigned managed identity* (UAMI) attached to the Function App.
   It authenticates directly to Azure Data Explorer and to Table Storage.

2. An *Entra app registration* that the UAMI is federated to via workload
   identity federation. Power Platform is reached only through this app
   registration, because tenant-wide environment listing requires an app that
   has been registered as a Power Platform management application, and because
   the Dataverse application user in each environment is bound to its client ID.

The federation exchange is: get a managed identity token whose audience is
``api://AzureADTokenExchange``, then present it as a ``client_assertion`` on the
app registration's client-credentials request. No client secret is involved.

Reference:
https://learn.microsoft.com/en-us/entra/workload-id/workload-identity-federation-config-app-trust-managed-identity
"""

from __future__ import annotations

import logging
from functools import lru_cache

from azure.core.credentials import TokenCredential
from azure.identity import (
    ClientAssertionCredential,
    DefaultAzureCredential,
    ManagedIdentityCredential,
)

from .settings import Settings

logger = logging.getLogger(__name__)

# Audience that must appear in the managed identity token presented for exchange.
# Sovereign clouds use AzureADTokenExchangeUSGov / AzureADTokenExchangeChina.
TOKEN_EXCHANGE_AUDIENCE = "api://AzureADTokenExchange"

# Documented scope for the legacy BAP admin surface. The double slash is not a
# typo -- it is what Microsoft documents, and the token request fails without it.
# https://learn.microsoft.com/en-us/power-platform/admin/programmability-authentication
POWERAPPS_SCOPE = "https://service.powerapps.com//.default"


def azure_credential(settings: Settings) -> TokenCredential:
    """Credential for Azure Data Explorer and Table Storage.

    A NEW instance is returned on every call, deliberately. The Kusto SDK closes
    the credential it was given when its client is closed, so sharing one
    instance across several clients leaves later consumers holding a closed
    transport and failing with "HTTP transport has already been closed".

    In Azure this resolves to the user-assigned managed identity. Locally,
    ``UAMI_CLIENT_ID`` is left blank and DefaultAzureCredential falls back to the
    developer's ``az login`` session.
    """
    if settings.uami_client_id:
        return ManagedIdentityCredential(client_id=settings.uami_client_id)
    logger.info(
        "UAMI_CLIENT_ID not set; falling back to DefaultAzureCredential for Azure resources."
    )
    return DefaultAzureCredential()


# Cached, unlike azure_credential. This one is only ever used through
# TokenProvider for HTTP calls we make ourselves, so nothing closes it. The
# Azure credential is handed to Kusto clients, which close it.
@lru_cache(maxsize=2)
def _power_platform_credential(
    tenant_id: str, app_client_id: str, uami_client_id: str
) -> TokenCredential:
    if not uami_client_id:
        logger.warning(
            "UAMI_CLIENT_ID not set; using DefaultAzureCredential for Power Platform. "
            "This authenticates as the signed-in developer rather than as the "
            "application user, so results will differ from what runs in Azure."
        )
        return DefaultAzureCredential()

    managed_identity = ManagedIdentityCredential(client_id=uami_client_id)

    def _assertion() -> str:
        # azure-identity caches the resulting app token; this inner call is cheap.
        return managed_identity.get_token(f"{TOKEN_EXCHANGE_AUDIENCE}/.default").token

    return ClientAssertionCredential(
        tenant_id=tenant_id,
        client_id=app_client_id,
        func=_assertion,
    )


def power_platform_credential(settings: Settings) -> TokenCredential:
    """Credential for the Power Platform BAP admin API and the Dataverse Web API."""
    return _power_platform_credential(
        settings.tenant_id, settings.app_client_id, settings.uami_client_id
    )


def dataverse_scope(environment_url: str) -> str:
    """Return the ``.default`` scope for a Dataverse organization.

    Confidential clients use ``<environment-url>/.default``.
    https://learn.microsoft.com/en-us/power-apps/developer/data-platform/authenticate-oauth
    """
    return f"{environment_url.rstrip('/')}/.default"
