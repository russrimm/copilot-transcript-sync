<#
.SYNOPSIS
    Grants the Function's managed identity the Microsoft Graph permission needed
    to resolve Entra users.

.DESCRIPTION
    The user dimension turns the Entra object IDs found in transcripts into
    departments and job titles, which is what makes org-level segmentation
    possible. That needs the User.Read.All application permission on Microsoft
    Graph, held by the managed identity.

    App role assignments to a managed identity cannot be made with
    'az ad app permission', so this uses the Graph API directly.

    Run once, as an administrator who can grant application permissions
    (Privileged Role Administrator, or Global Administrator).

.EXAMPLE
    ./scripts/grant_graph_permission.ps1 -ResourceGroup rg-copilot-transcripts -IdentityName cts-id-abc123

.EXAMPLE
    ./scripts/grant_graph_permission.ps1 -PrincipalId 00000000-0000-0000-0000-000000000000
#>

[CmdletBinding()]
param(
    [string] $ResourceGroup = 'rg-copilot-transcripts',
    [string] $IdentityName,

    # Object (principal) ID of the managed identity. Resolved from the identity
    # name when omitted.
    [string] $PrincipalId,

    [string] $Permission = 'User.Read.All',
    [switch] $Remove
)

$ErrorActionPreference = 'Stop'

$GRAPH_APP_ID = '00000003-0000-0000-c000-000000000000'

if (-not $PrincipalId) {
    if (-not $IdentityName) {
        $IdentityName = az identity list -g $ResourceGroup --query "[0].name" -o tsv
        if (-not $IdentityName) {
            throw "No managed identity found in '$ResourceGroup'. Pass -IdentityName or -PrincipalId."
        }
    }
    $PrincipalId = az identity show -g $ResourceGroup -n $IdentityName --query principalId -o tsv
    if (-not $PrincipalId) { throw "Could not resolve the principal ID for '$IdentityName'." }
}

Write-Host "Managed identity principal: $PrincipalId" -ForegroundColor Cyan

# --- Resolve the Graph service principal and the app role ------------------

$graphSpId = az ad sp list --filter "appId eq '$GRAPH_APP_ID'" --query "[0].id" -o tsv
if (-not $graphSpId) { throw "Microsoft Graph service principal not found in this tenant." }

$roleId = az ad sp show --id $graphSpId `
    --query "appRoles[?value=='$Permission' && contains(allowedMemberTypes,'Application')].id | [0]" -o tsv
if (-not $roleId) { throw "No application app role named '$Permission' on Microsoft Graph." }

Write-Host "Graph app role '$Permission': $roleId"

# --- Existing assignment? --------------------------------------------------

$existing = az rest --method GET `
    --uri "https://graph.microsoft.com/v1.0/servicePrincipals/$PrincipalId/appRoleAssignments" `
    --query "value[?appRoleId=='$roleId'].id | [0]" -o tsv 2>$null

if ($Remove) {
    if (-not $existing) {
        Write-Host "Nothing to remove: '$Permission' is not assigned." -ForegroundColor Yellow
        return
    }
    az rest --method DELETE `
        --uri "https://graph.microsoft.com/v1.0/servicePrincipals/$PrincipalId/appRoleAssignments/$existing" -o none
    Write-Host "Removed '$Permission' from the managed identity." -ForegroundColor Green
    return
}

if ($existing) {
    Write-Host "'$Permission' is already assigned." -ForegroundColor Yellow
    return
}

# --- Assign ----------------------------------------------------------------

$payload = @{
    principalId = $PrincipalId
    resourceId  = $graphSpId
    appRoleId   = $roleId
} | ConvertTo-Json -Compress

$tempFile = New-TemporaryFile
try {
    $payload | Set-Content -Path $tempFile -Encoding utf8
    az rest --method POST `
        --uri "https://graph.microsoft.com/v1.0/servicePrincipals/$PrincipalId/appRoleAssignments" `
        --headers "Content-Type=application/json" `
        --body "@$tempFile" -o none
}
finally {
    Remove-Item $tempFile -ErrorAction SilentlyContinue
}

Write-Host ""
Write-Host "Granted '$Permission' to the managed identity." -ForegroundColor Green
Write-Host ""
Write-Host "This resolves personal data -- names, job titles, locations -- into the" -ForegroundColor Yellow
Write-Host "analytics store. Enable the sync only if that is acceptable:" -ForegroundColor Yellow
Write-Host ""
Write-Host "  az functionapp config appsettings set -g $ResourceGroup -n <app> --settings SYNC_USERS=true"
Write-Host ""
