<#
.SYNOPSIS
    Removes everything the transcript sync created, including the tenant-level
    objects that deleting the resource group leaves behind.

.DESCRIPTION
    Four things are removed, in an order that matters:

      1. Dataverse application users in every environment. Done first, because
         it needs the app registration to still exist in order to enumerate
         environments and authenticate to each one.
      2. The Power Platform management application registration.
      3. The Entra app registration and its service principal.
      4. The Azure resource group.

    Deleting the resource group alone leaves 1-3 behind: an app registration
    with tenant-wide Power Platform rights and a System Administrator
    application user in every environment.

.EXAMPLE
    ./scripts/uninstall.ps1

.EXAMPLE
    ./scripts/uninstall.ps1 -Force

.EXAMPLE
    # Report what would be removed, change nothing
    ./scripts/uninstall.ps1 -WhatIf
#>

[CmdletBinding()]
param(
    [string] $ResourceGroup = 'rg-copilot-transcripts',
    [string] $AppDisplayName = 'copilot-transcript-sync',

    # Optional. Resolved from -AppDisplayName when omitted. Pass it explicitly if
    # the app registration was already deleted and application users remain.
    [string] $AppClientId,

    [switch] $KeepResourceGroup,
    [switch] $KeepAppRegistration,
    [switch] $Force,

    # Declared explicitly rather than via SupportsShouldProcess, so the behavior
    # is identical whether the script is dot-sourced or run with `pwsh -File`.
    [switch] $WhatIf
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot

if ($WhatIf) { $WhatIfPreference = $true }

# Prefer the repo virtual environment. Falling through to a bare `python` picks
# up an interpreter without azure-identity installed, and the application-user
# cleanup then fails at import time.
function Get-PythonPath {
    $venv = Join-Path $repoRoot '.venv/Scripts/python.exe'
    if (Test-Path $venv) { return $venv }
    $venvNix = Join-Path $repoRoot '.venv/bin/python'
    if (Test-Path $venvNix) { return $venvNix }
    return 'python'
}

# $PSCmdlet.ShouldProcess is unreliable when the script is invoked with
# `pwsh -File`: the variable resolves but its command runtime is not initialized,
# so calling it throws a null reference. Handle -WhatIf directly instead.
function Confirm-Action {
    param([string] $Target, [string] $Action)
    if ($WhatIfPreference) {
        Write-Host "What if: $Action -> $Target"
        return $false
    }
    return $true
}

function Write-Phase { param([string] $m) Write-Host ""; Write-Host $m -ForegroundColor Cyan }
function Write-Ok    { param([string] $m) Write-Host "  $m" -ForegroundColor Green }
function Write-Info  { param([string] $m) Write-Host "  $m" }
function Write-Warn2 { param([string] $m) Write-Host "  $m" -ForegroundColor Yellow }

$account = az account show 2>$null | ConvertFrom-Json
if (-not $account) {
    throw "Not signed in. Run 'az login' as a Power Platform or Global Administrator first."
}

if (-not $AppClientId) {
    $AppClientId = az ad app list --display-name $AppDisplayName --query "[0].appId" -o tsv
}

# ---------------------------------------------------------------------------
# Inventory first, so the confirmation prompt is specific
# ---------------------------------------------------------------------------

Write-Host ""
Write-Host "Copilot Studio transcript sync - uninstall" -ForegroundColor Cyan
Write-Host ""
Write-Info "Subscription : $($account.name)"
Write-Info "Tenant       : $($account.tenantId)"
Write-Host ""

$rgExists = (az group exists --name $ResourceGroup) -eq 'true'
$resourceCount = 0
if ($rgExists) {
    $resourceCount = (az resource list -g $ResourceGroup --query "[].name" -o tsv | Measure-Object).Count
}

$mgmtRegistered = $false
if ($AppClientId) {
    try {
        $token = az account get-access-token --resource 'https://service.powerapps.com/' --query accessToken -o tsv
        $apps = Invoke-RestMethod -Headers @{ Authorization = "Bearer $token" } `
            -Uri 'https://api.bap.microsoft.com/providers/Microsoft.BusinessAppPlatform/adminApplications?api-version=2020-10-01'
        $mgmtRegistered = $apps.value.applicationId -contains $AppClientId
    }
    catch { Write-Warn2 "Could not query Power Platform management applications: $($_.Exception.Message)" }
}

Write-Host "Will remove:" -ForegroundColor Yellow
Write-Info ("  Resource group '{0}'{1}" -f $ResourceGroup, $(
    if (-not $rgExists) { ' - not found' }
    elseif ($KeepResourceGroup) { ' - KEEPING (-KeepResourceGroup)' }
    else { " - $resourceCount resources" }))
Write-Info ("  Dataverse application users for {0}" -f $(if ($AppClientId) { $AppClientId } else { 'unknown client ID - skipped' }))
Write-Info ("  Power Platform management app registration{0}" -f $(if ($mgmtRegistered) { '' } else { ' - not registered' }))
Write-Info ("  Entra app registration '{0}'{1}" -f $AppDisplayName, $(
    if (-not $AppClientId) { ' - not found' }
    elseif ($KeepAppRegistration) { ' - KEEPING (-KeepAppRegistration)' }
    else { '' }))
Write-Host ""

if (-not $Force -and -not $WhatIfPreference) {
    $answer = Read-Host "Type the resource group name to confirm ($ResourceGroup)"
    if ($answer -ne $ResourceGroup) {
        Write-Host "Cancelled." -ForegroundColor Yellow
        return
    }
}

# ---------------------------------------------------------------------------
# 1. Dataverse application users
#
# First, because it needs the app registration to still exist.
# ---------------------------------------------------------------------------

if ($AppClientId) {
    Write-Phase "1/4 Removing Dataverse application users"
    if (Confirm-Action "application users for $AppClientId" "Delete across all environments") {
        $python = Get-PythonPath
        & $python (Join-Path $repoRoot 'scripts/cleanup_app_users.py') --client-id $AppClientId --delete
        if ($LASTEXITCODE -ne 0) {
            # Deliberately fatal. Continuing would delete the app registration,
            # and the application users are far harder to find and remove once
            # the registration they are bound to no longer exists.
            throw @"
Application user cleanup failed, so the uninstall stopped before deleting the
app registration. Removing the registration now would orphan a System
Administrator application user in every environment.

Fix the cause, then re-run. If the interpreter could not import azure-identity,
create the virtual environment first:

  python -m venv .venv
  .venv/Scripts/python.exe -m pip install -r requirements.txt

To skip this step deliberately and accept the orphaned users, re-run with
-AppClientId '' and clean them up later with:

  python scripts/cleanup_app_users.py --client-id $AppClientId --delete
"@
        }
    }
}
else {
    Write-Phase "1/4 Skipping application users - no client ID"
    Write-Warn2 "Pass -AppClientId to clean these up if the app registration is already gone."
}

# ---------------------------------------------------------------------------
# 2. Power Platform management application
# ---------------------------------------------------------------------------

Write-Phase "2/4 Unregistering the Power Platform management application"
if ($mgmtRegistered) {
    if (Confirm-Action $AppClientId "Unregister management application") {
        try {
            $token = az account get-access-token --resource 'https://service.powerapps.com/' --query accessToken -o tsv
            Invoke-RestMethod -Method DELETE -Headers @{ Authorization = "Bearer $token" } `
                -Uri "https://api.bap.microsoft.com/providers/Microsoft.BusinessAppPlatform/adminApplications/$AppClientId`?api-version=2020-10-01" | Out-Null
            Write-Ok "Unregistered."
        }
        catch {
            Write-Warn2 "REST unregistration failed: $($_.Exception.Message)"
            Write-Warn2 "Fall back to: Remove-PowerAppManagementApp -ApplicationId $AppClientId"
        }
    }
}
else {
    Write-Info "Nothing to unregister."
}

# ---------------------------------------------------------------------------
# 3. Entra app registration
# ---------------------------------------------------------------------------

Write-Phase "3/4 Deleting the Entra app registration"
if ($AppClientId -and -not $KeepAppRegistration) {
    if (Confirm-Action $AppClientId "Delete app registration") {
        az ad app delete --id $AppClientId 2>&1 | Out-Null
        if ($LASTEXITCODE -eq 0) { Write-Ok "Deleted $AppClientId." }
        else { Write-Warn2 "Could not delete the app registration. Remove it manually." }
    }
}
else {
    Write-Info "Skipped."
}

# ---------------------------------------------------------------------------
# 4. Azure resource group
# ---------------------------------------------------------------------------

Write-Phase "4/4 Deleting the Azure resource group"
if ($rgExists -and -not $KeepResourceGroup) {
    if (Confirm-Action $ResourceGroup "Delete resource group and all resources") {
        Write-Info "Deleting '$ResourceGroup' ($resourceCount resources). This takes several minutes..."
        az group delete --name $ResourceGroup --yes 2>&1 | Out-Null
        if ($LASTEXITCODE -eq 0) { Write-Ok "Deleted." }
        else { throw "Could not delete resource group '$ResourceGroup'." }
    }
}
else {
    Write-Info "Skipped."
}

# ---------------------------------------------------------------------------
# Verify
# ---------------------------------------------------------------------------

if (-not $WhatIfPreference) {
    Write-Phase "Verifying"
    $remaining = @()

    if ((az group exists --name $ResourceGroup) -eq 'true' -and -not $KeepResourceGroup) {
        $remaining += "resource group '$ResourceGroup' still exists"
    }
    if (-not $KeepAppRegistration) {
        $still = az ad app list --display-name $AppDisplayName --query "[0].appId" -o tsv 2>$null
        if ($still) { $remaining += "app registration $still still exists" }
    }

    if ($remaining) {
        Write-Host ""
        foreach ($item in $remaining) { Write-Warn2 $item }
    }
    else {
        Write-Ok "Nothing left behind."
    }
}

Write-Host ""
Write-Host "Uninstall complete." -ForegroundColor Green
Write-Host ""
