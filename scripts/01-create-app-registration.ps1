<#
.SYNOPSIS
    Phase 1 of the one-time bootstrap: create the Entra app registration that
    reaches Power Platform and Dataverse.

.DESCRIPTION
    Run this BEFORE deploying infra/main.bicep, because the template needs the
    resulting client ID as its powerPlatformAppClientId parameter.

    No client secret is created. The app registration is credential-less until
    phase 2 federates the Function's managed identity onto it.

.NOTES
    Requires: Azure CLI, signed in as a user who can create app registrations
    (Application Administrator, Application Developer, or Cloud Application
    Administrator).
#>

[CmdletBinding()]
param(
    [string] $DisplayName = 'copilot-transcript-sync',

    # Entra app ID of the first-party "Power Apps Service" resource. The service
    # principal must exist in the tenant before a token can be issued for the
    # https://service.powerapps.com//.default scope.
    # https://learn.microsoft.com/en-us/power-platform/admin/programmability-authentication
    [string] $PowerAppsServiceAppId = '475226c6-020e-4fb2-8a90-7a972cbfc1d4'
)

$ErrorActionPreference = 'Stop'

Write-Host "Signed in as:" -ForegroundColor Cyan
az account show --query "{tenant:tenantId, user:user.name}" -o tsv

# --- App registration -----------------------------------------------------
$existing = az ad app list --display-name $DisplayName --query "[0].appId" -o tsv

if ($existing) {
    Write-Host "App registration '$DisplayName' already exists: $existing" -ForegroundColor Yellow
    $appId = $existing
}
else {
    Write-Host "Creating app registration '$DisplayName'..." -ForegroundColor Cyan
    # Single tenant. No redirect URI: this app never performs an interactive
    # sign-in, it is only ever reached through workload identity federation.
    $appId = az ad app create `
        --display-name $DisplayName `
        --sign-in-audience AzureADMyOrg `
        --query appId -o tsv
    Write-Host "Created app registration: $appId" -ForegroundColor Green
}

# --- Service principal for the app ---------------------------------------
$appSpId = az ad sp list --filter "appId eq '$appId'" --query "[0].id" -o tsv
if (-not $appSpId) {
    Write-Host "Creating service principal for the app..." -ForegroundColor Cyan
    $appSpId = az ad sp create --id $appId --query id -o tsv
}
Write-Host "App service principal object ID: $appSpId"

# --- Power Apps Service resource principal --------------------------------
# Entra will not issue a token for a resource whose service principal is absent
# from the tenant, and in many tenants this one has never been provisioned.
$ppSpId = az ad sp list --filter "appId eq '$PowerAppsServiceAppId'" --query "[0].id" -o tsv
if (-not $ppSpId) {
    Write-Host "Provisioning the Power Apps Service resource principal..." -ForegroundColor Cyan
    $ppSpId = az ad sp create --id $PowerAppsServiceAppId --query id -o tsv
    Write-Host "Created: $ppSpId" -ForegroundColor Green
}
else {
    Write-Host "Power Apps Service resource principal present: $ppSpId"
}

Write-Host ""
Write-Host "Phase 1 complete." -ForegroundColor Green
Write-Host "-------------------------------------------------------------"
Write-Host "  Application (client) ID : $appId"
Write-Host "  Tenant ID               : $(az account show --query tenantId -o tsv)"
Write-Host "-------------------------------------------------------------"
Write-Host ""
Write-Host "Next: deploy the infrastructure, passing the client ID:" -ForegroundColor Cyan
Write-Host "  az deployment group create ``"
Write-Host "    --resource-group <rg> ``"
Write-Host "    --template-file infra/main.bicep ``"
Write-Host "    --parameters powerPlatformAppClientId=$appId adxAdminPrincipalId=<your-object-id>"
Write-Host ""
Write-Host "Then run scripts/02-federate-and-register.ps1." -ForegroundColor Cyan
