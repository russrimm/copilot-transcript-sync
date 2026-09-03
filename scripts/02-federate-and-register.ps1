<#
.SYNOPSIS
    Phase 2 of the one-time bootstrap: federate the Function's managed identity
    onto the app registration, and register the app as a Power Platform
    management application.

.DESCRIPTION
    Run this AFTER infra/main.bicep has been deployed, because the federated
    identity credential's subject must be the managed identity's object
    (principal) ID, which the template outputs as identityPrincipalId.

    Two things happen here:

    1. A federated identity credential is added to the app registration, trusting
       the user-assigned managed identity. This is what lets the Function
       authenticate as the app without any secret.
       https://learn.microsoft.com/en-us/entra/workload-id/workload-identity-federation-config-app-trust-managed-identity

    2. The app is registered as a Power Platform management application. Without
       this, app-only calls to the BAP admin environment endpoint return 403. A
       service principal cannot perform this registration for itself -- by design
       it requires an interactive administrator.
       https://learn.microsoft.com/en-us/power-platform/admin/powershell-create-service-principal

.NOTES
    Requires: Azure CLI signed in with rights to update application credentials,
    and a Power Platform Administrator or Global Administrator for step 2.
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory)] [string] $AppClientId,

    # Bicep output: identityPrincipalId. This is the managed identity's OBJECT
    # (principal) ID, not its client ID. Using the client ID here creates the
    # credential successfully but every token exchange then fails.
    [Parameter(Mandatory)] [string] $ManagedIdentityPrincipalId,

    [string] $CredentialName = 'copilot-transcript-sync-uami',
    [string] $TenantId,
    [switch] $SkipManagementAppRegistration
)

$ErrorActionPreference = 'Stop'

if (-not $TenantId) {
    $TenantId = az account show --query tenantId -o tsv
}

# --- 1. Federated identity credential -------------------------------------

$appObjectId = az ad app list --filter "appId eq '$AppClientId'" --query "[0].id" -o tsv
if (-not $appObjectId) {
    throw "No app registration found with client ID $AppClientId. Run 01-create-app-registration.ps1 first."
}

$existing = az ad app federated-credential list --id $appObjectId `
    --query "[?name=='$CredentialName'] | [0].name" -o tsv

if ($existing) {
    Write-Host "Federated credential '$CredentialName' already exists." -ForegroundColor Yellow
}
else {
    $credential = @{
        name        = $CredentialName
        issuer      = "https://login.microsoftonline.com/$TenantId/v2.0"
        subject     = $ManagedIdentityPrincipalId
        audiences   = @('api://AzureADTokenExchange')
        description = 'Trusts the transcript sync Function App user-assigned managed identity.'
    }

    $tempFile = New-TemporaryFile
    try {
        $credential | ConvertTo-Json -Depth 5 | Set-Content -Path $tempFile -Encoding utf8
        Write-Host "Creating federated identity credential..." -ForegroundColor Cyan
        az ad app federated-credential create --id $appObjectId --parameters "@$tempFile" | Out-Null
        Write-Host "Created federated credential '$CredentialName'." -ForegroundColor Green
    }
    finally {
        Remove-Item $tempFile -ErrorAction SilentlyContinue
    }
}

Write-Warning @'
Entra accepts an incorrect issuer, subject, or audience without complaint. The
mistake only surfaces later as a failed token exchange at runtime. Verify the
subject above matches the managed identity's Object (principal) ID exactly.
'@

# --- 2. Power Platform management application ------------------------------

if ($SkipManagementAppRegistration) {
    Write-Host "Skipping Power Platform management app registration as requested." -ForegroundColor Yellow
    return
}

Write-Host ""
Write-Host "Registering the app as a Power Platform management application..." -ForegroundColor Cyan

# The documented route is New-PowerAppManagementApp from
# Microsoft.PowerApps.Administration.PowerShell, which requires an interactive
# sign-in. The REST equivalent accepts the Azure CLI's delegated token, so try
# that first and fall back to the module only if it fails.
$registered = $false

try {
    $token = az account get-access-token --resource 'https://service.powerapps.com/' --query accessToken -o tsv
    if ($LASTEXITCODE -ne 0 -or -not $token) { throw 'Could not acquire a Power Apps Service token via Azure CLI.' }

    $headers = @{ Authorization = "Bearer $token"; Accept = 'application/json' }
    $uri = "https://api.bap.microsoft.com/providers/Microsoft.BusinessAppPlatform/adminApplications/$AppClientId" +
           "?api-version=2020-10-01"

    Invoke-RestMethod -Method PUT -Uri $uri -Headers $headers -ContentType 'application/json' | Out-Null

    $current = Invoke-RestMethod -Method GET -Headers $headers `
        -Uri "https://api.bap.microsoft.com/providers/Microsoft.BusinessAppPlatform/adminApplications?api-version=2020-10-01"

    if ($current.value.applicationId -contains $AppClientId) {
        Write-Host "Registered $AppClientId as a Power Platform management application." -ForegroundColor Green
        $registered = $true
    }
}
catch {
    Write-Warning "REST registration failed: $($_.Exception.Message)"
    if ($_.ErrorDetails) { Write-Warning $_.ErrorDetails.Message }
}

if (-not $registered) {
    Write-Host "Falling back to Microsoft.PowerApps.Administration.PowerShell..." -ForegroundColor Cyan

    if (-not (Get-Module -ListAvailable -Name Microsoft.PowerApps.Administration.PowerShell)) {
        Install-Module -Name Microsoft.PowerApps.Administration.PowerShell -Scope CurrentUser -Force -AllowClobber
    }

    Import-Module Microsoft.PowerApps.Administration.PowerShell

    Write-Host "Sign in as a Power Platform Administrator or Global Administrator when prompted." -ForegroundColor Yellow
    Add-PowerAppsAccount -Endpoint prod -TenantID $TenantId
    New-PowerAppManagementApp -ApplicationId $AppClientId
    Get-PowerAppManagementApp -ApplicationId $AppClientId
}

Write-Host ""
Write-Host "Phase 2 complete." -ForegroundColor Green
Write-Host "The Function can now list every environment in the tenant app-only," -ForegroundColor Green
Write-Host "and will provision its own Dataverse application users on first run." -ForegroundColor Green
