<#
.SYNOPSIS
    Installs the Copilot Studio transcript sync end to end.

.DESCRIPTION
    Chains the seven steps documented in the README, passing outputs between
    them so you do not have to copy client IDs and principal IDs by hand.

    Each step is idempotent where the underlying service allows it, so a failed
    run can be restarted, or resumed from a specific step with -FromStep.

    Two things are deliberately not automated:

      * Sign-in. The bootstrap requires an interactive administrator, because a
        service principal cannot register itself as a Power Platform management
        application. Run `az login` as an administrator first.
      * Choosing a region. Azure Data Explorer capacity varies, so the script
        retries the SKUs in -AdxSkuCandidates order but will not silently move
        the deployment to a different region.

.EXAMPLE
    ./scripts/install.ps1

.EXAMPLE
    ./scripts/install.ps1 -ResourceGroup rg-transcripts -Location westus2

.EXAMPLE
    # Resume after fixing a failure in step 5
    ./scripts/install.ps1 -FromStep 5
#>

[CmdletBinding()]
param(
    [string] $ResourceGroup = 'rg-copilot-transcripts',
    [string] $Location = 'eastus',
    [string] $AppDisplayName = 'copilot-transcript-sync',
    [string] $NamePrefix = 'cts',

    # Tried in order. Azure Data Explorer capacity is regional and transient, so
    # a single SKU failing does not mean the deployment is wrong.
    [string[]] $AdxSkuCandidates = @(
        'Dev(No SLA)_Standard_E2a_v4',
        'Dev(No SLA)_Standard_D11_v2'
    ),

    [ValidateRange(1, 7)]
    [int] $FromStep = 1,

    [switch] $SkipFirstRun
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot

# Prefer the repo virtual environment. A bare `python` may be an interpreter
# without this project's dependencies installed.
function Get-PythonPath {
    $venv = Join-Path $repoRoot '.venv/Scripts/python.exe'
    if (Test-Path $venv) { return $venv }
    $venvNix = Join-Path $repoRoot '.venv/bin/python'
    if (Test-Path $venvNix) { return $venvNix }
    return 'python'
}

function Write-Step {
    param([int] $Number, [string] $Title)
    Write-Host ""
    Write-Host ("=" * 72) -ForegroundColor DarkGray
    Write-Host " Step $Number of 7 - $Title" -ForegroundColor Cyan
    Write-Host ("=" * 72) -ForegroundColor DarkGray
}

function Write-Ok    { param([string] $m) Write-Host "  $m" -ForegroundColor Green }
function Write-Info  { param([string] $m) Write-Host "  $m" }
function Write-Warn2 { param([string] $m) Write-Host "  $m" -ForegroundColor Yellow }

function Invoke-Native {
    param([scriptblock] $Command, [string] $ErrorMessage)
    $output = & $Command 2>&1
    if ($LASTEXITCODE -ne 0) {
        Write-Host ($output | Out-String) -ForegroundColor Red
        throw $ErrorMessage
    }
    return $output
}

# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------

Write-Host ""
Write-Host "Copilot Studio transcript sync - install" -ForegroundColor Cyan
Write-Host ""

foreach ($tool in 'az', 'func') {
    if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) {
        throw "Required tool '$tool' was not found on PATH."
    }
}

$pythonPath = Get-PythonPath
& $pythonPath -c "import azure.identity, azure.kusto.data" 2>$null
if ($LASTEXITCODE -ne 0) {
    throw @"
The Python interpreter at '$pythonPath' cannot import this project's dependencies.
Create the virtual environment first, so steps 4 and 7 can run:

  python -m venv .venv
  .venv/Scripts/python.exe -m pip install -r requirements.txt
"@
}

$account = az account show 2>$null | ConvertFrom-Json
if (-not $account) {
    throw "Not signed in. Run 'az login' as a Power Platform or Global Administrator first."
}

$tenantId = $account.tenantId
Write-Info "Subscription : $($account.name)"
Write-Info "Tenant       : $tenantId"
Write-Info "Signed in as : $($account.user.name)"

if ($env:AZURE_CLIENT_ID -and $env:AZURE_CLIENT_SECRET) {
    Write-Warn2 ""
    Write-Warn2 "AZURE_CLIENT_ID and AZURE_CLIENT_SECRET are set in this shell."
    Write-Warn2 "DefaultAzureCredential resolves those ahead of your az login session, so"
    Write-Warn2 "any tool using it will authenticate as that service principal instead of you."
}

# ---------------------------------------------------------------------------
# Step 1 - App registration
# ---------------------------------------------------------------------------

if ($FromStep -le 1) {
    Write-Step 1 "Create the Entra app registration"
    & (Join-Path $PSScriptRoot '01-create-app-registration.ps1') -DisplayName $AppDisplayName
    if ($LASTEXITCODE -ne 0) { throw "Step 1 failed." }
}

$appId = az ad app list --display-name $AppDisplayName --query "[0].appId" -o tsv
if (-not $appId) { throw "No app registration named '$AppDisplayName' was found." }
Write-Ok "Application (client) ID: $appId"

# ---------------------------------------------------------------------------
# Step 2 - Infrastructure
# ---------------------------------------------------------------------------

if ($FromStep -le 2) {
    Write-Step 2 "Deploy the Azure infrastructure"

    Write-Info "Ensuring resource group '$ResourceGroup' in $Location..."
    Invoke-Native { az group create --name $ResourceGroup --location $Location -o none } `
        "Could not create resource group '$ResourceGroup'."

    $deployed = $false
    foreach ($sku in $AdxSkuCandidates) {
        Write-Info "Deploying with Azure Data Explorer SKU '$sku' (this takes 10-15 minutes)..."

        $deployName = "cts-install-$(Get-Date -Format 'yyyyMMddHHmmss')"
        $output = az deployment group create `
            --resource-group $ResourceGroup `
            --name $deployName `
            --template-file (Join-Path $repoRoot 'infra/main.bicep') `
            --parameters powerPlatformAppClientId=$appId `
                         namePrefix=$NamePrefix `
                         adxSkuName=$sku `
            -o json 2>&1

        if ($LASTEXITCODE -eq 0) {
            $deployed = $true
            Write-Ok "Infrastructure deployed."
            break
        }

        $text = $output | Out-String
        if ($text -match 'InsufficientResourcesForSubscription') {
            Write-Warn2 "No capacity for '$sku' in $Location right now. Trying the next SKU."
            continue
        }

        Write-Host $text -ForegroundColor Red
        throw "Step 2 failed for a reason other than Azure Data Explorer capacity."
    }

    if (-not $deployed) {
        throw @"
Every Azure Data Explorer SKU candidate was refused for capacity in '$Location'.
List what this subscription can place in the region, then re-run with
-AdxSkuCandidates, or choose another -Location:

  `$sub = az account show --query id -o tsv
  `$t = az account get-access-token --resource https://management.azure.com/ --query accessToken -o tsv
  (Invoke-RestMethod -Headers @{Authorization = "Bearer `$t"} ``
    -Uri "https://management.azure.com/subscriptions/`$sub/providers/Microsoft.Kusto/skus?api-version=2024-04-13").value |
    Where-Object { `$_.resourceType -eq 'clusters' -and `$_.locations -contains '$Location' } |
    ForEach-Object { `$_.name } | Sort-Object -Unique
"@
    }
}

# Read the outputs from whichever deployment last succeeded.
$lastDeployment = az deployment group list --resource-group $ResourceGroup `
    --query "sort_by([?properties.provisioningState=='Succeeded'], &properties.timestamp)[-1].name" -o tsv
if (-not $lastDeployment) { throw "No successful deployment found in '$ResourceGroup'." }

$outputs = az deployment group show --resource-group $ResourceGroup --name $lastDeployment `
    --query properties.outputs -o json | ConvertFrom-Json

$identityPrincipalId = $outputs.identityPrincipalId.value
$adxClusterUri       = $outputs.adxClusterUri.value
$adxDatabase         = $outputs.adxDatabaseName.value
$functionAppName     = $outputs.functionAppName.value

Write-Ok "Managed identity principal ID: $identityPrincipalId"
Write-Ok "Azure Data Explorer cluster  : $adxClusterUri"
Write-Ok "Function App                 : $functionAppName"

# ---------------------------------------------------------------------------
# Step 3 - Federation and Power Platform registration
# ---------------------------------------------------------------------------

if ($FromStep -le 3) {
    Write-Step 3 "Federate the managed identity and register with Power Platform"
    & (Join-Path $PSScriptRoot '02-federate-and-register.ps1') `
        -AppClientId $appId `
        -ManagedIdentityPrincipalId $identityPrincipalId `
        -TenantId $tenantId
    if ($LASTEXITCODE -ne 0) { throw "Step 3 failed." }
}

# ---------------------------------------------------------------------------
# Step 4 - Azure Data Explorer schema
# ---------------------------------------------------------------------------

if ($FromStep -le 4) {
    Write-Step 4 "Create the Azure Data Explorer schema"
    $python = Get-PythonPath
    Invoke-Native {
        & $python (Join-Path $repoRoot 'scripts/deploy_kql.py') `
            --cluster $adxClusterUri --database $adxDatabase
    } "Step 4 failed. Check that you hold Admin on the database."
    Write-Ok "Schema applied."
}

# ---------------------------------------------------------------------------
# Step 5 - Publish
# ---------------------------------------------------------------------------

if ($FromStep -le 5) {
    Write-Step 5 "Publish the Function"

    # Vendor Linux wheels locally. The remote build cannot reach a storage
    # account whose public network access is disabled, and building here works
    # regardless of that policy.
    $packageDir = Join-Path $repoRoot '.python_packages/lib/site-packages'
    $python = Get-PythonPath
    Write-Info "Vendoring Linux dependencies..."
    if (Test-Path $packageDir) { Remove-Item -Recurse -Force $packageDir }

    Invoke-Native {
        & $python -m pip install `
            --target $packageDir `
            --platform manylinux2014_x86_64 `
            --only-binary=:all: `
            --python-version 3.13 `
            -r (Join-Path $repoRoot 'requirements.txt') --quiet
    } "Could not vendor dependencies for the Linux runtime."

    $count = (Get-ChildItem $packageDir -Directory).Count
    Write-Ok "Vendored $count packages."

    Push-Location $repoRoot
    try {
        Write-Info "Publishing (local build)..."
        func azure functionapp publish $functionAppName --python --no-build | Out-Host
        if ($LASTEXITCODE -ne 0) { throw "Step 5 failed during publish." }
    }
    finally { Pop-Location }

    $functions = az functionapp function list -g $ResourceGroup -n $functionAppName `
        --query "[].name" -o tsv 2>$null
    if (-not $functions) {
        throw @"
Publish reported success but the app has no functions. This happens when the
dependencies did not ship, because the worker then cannot import azure.functions
and indexing yields nothing. Check $packageDir and re-run with -FromStep 5.
"@
    }
    Write-Ok "Functions registered: $(($functions -split "`n" | ForEach-Object { ($_ -split '/')[-1] }) -join ', ')"
}

# ---------------------------------------------------------------------------
# Step 6 - First run
# ---------------------------------------------------------------------------

if ($FromStep -le 6 -and -not $SkipFirstRun) {
    Write-Step 6 "Trigger the first sync"

    $key = az functionapp function keys list -g $ResourceGroup -n $functionAppName `
        --function-name TranscriptSyncManual --query default -o tsv
    if (-not $key) { throw "Could not read the function key for TranscriptSyncManual." }

    Write-Info "Running a full tenant pass. This can take a few minutes..."
    try {
        $result = Invoke-RestMethod -Method POST -TimeoutSec 1800 `
            -Uri "https://$functionAppName.azurewebsites.net/api/sync?code=$key"
    }
    catch { throw "Step 6 failed to invoke the sync: $($_.Exception.Message)" }

    Write-Ok ("Discovered {0} environments, synced {1}, failed {2}, ingested {3} rows." -f `
        $result.environments_discovered, $result.environments_synced,
        $result.environments_failed, $result.rows_ingested)

    foreach ($e in $result.environments) {
        $suffix = if ($e.error) { "ERROR: $($e.error)" } elseif ($e.skipped) { "no new transcripts" } else { "" }
        Write-Info ("  {0,-30} {1,-12} rows={2,-5} {3}" -f $e.environment_name, $e.environment_type, $e.rows, $suffix)
    }

    if ($result.environments_failed -gt 0) {
        Write-Warn2 "Some environments failed. They are reported above and will be retried next run."
    }
}

# ---------------------------------------------------------------------------
# Step 7 - Verify
# ---------------------------------------------------------------------------

if ($FromStep -le 7) {
    Write-Step 7 "Verify"
    Write-Info "Waiting for Azure Data Explorer ingestion batching (about 5 minutes)..."
    Start-Sleep -Seconds 300

    $python = Get-PythonPath
    Invoke-Native {
        & $python (Join-Path $repoRoot 'scripts/verify_install.py') `
            --cluster $adxClusterUri --database $adxDatabase
    } "Step 7 verification failed."
}

# ---------------------------------------------------------------------------

Write-Host ""
Write-Host ("=" * 72) -ForegroundColor DarkGray
Write-Host " Install complete" -ForegroundColor Green
Write-Host ("=" * 72) -ForegroundColor DarkGray
Write-Host ""
Write-Info "Resource group  : $ResourceGroup"
Write-Info "Function App    : $functionAppName"
Write-Info "Client ID       : $appId"
Write-Info "ADX cluster     : $adxClusterUri"
Write-Info "Database        : $adxDatabase"
Write-Host ""
Write-Info "The timer now runs every 30 minutes. To remove everything:"
Write-Info "  ./scripts/uninstall.ps1 -ResourceGroup $ResourceGroup -AppClientId $appId"
Write-Host ""
