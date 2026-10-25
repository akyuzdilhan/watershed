<#
.SYNOPSIS
Deploys the Watershed kill switch (Automation runbook + webhook + action group) and wires it to the subscription budget.

.DESCRIPTION
Run by hand, signed in with `az login` as Owner (or User Access Administrator + Contributor) of the subscription.
Pass 1 deploys the resource group, Automation account, empty runbook, custom role and its two assignments.
The script then uploads and publishes the runbook and asks Automation for a webhook URI.
Pass 2 deploys the webhook, the action group and the budget with the kill switch notifications.
The webhook URI is a secret (anyone holding it can start the runbook): it only goes through a secure parameter and is never printed.

.EXAMPLE
./infra/killswitch/deploy.ps1 -AlertEmail you@example.com
#>
param(
    [Parameter(Mandatory = $true)] [string[]] $AlertEmail,
    [string] $Location = 'swedencentral',
    [string] $BudgetName = 'hackathon-budget',
    [switch] $WhatIf
)

$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$template = Join-Path $here 'main.bicep'
$runbookFile = Join-Path $here 'Stop-Watershed.ps1'
$subscriptionId = az account show --query id -o tsv
if (-not $subscriptionId) { throw 'Run az login first.' }
Write-Host "Subscription: $subscriptionId"

$tmp = New-Item -ItemType Directory -Force -Path (Join-Path ([IO.Path]::GetTempPath()) "ws-killswitch-$PID")
function Write-Params([hashtable] $values) {
    $doc = @{ '$schema' = 'https://schema.management.azure.com/schemas/2019-04-01/deploymentParameters.json#'; contentVersion = '1.0.0.0'; parameters = @{} }
    foreach ($k in $values.Keys) { $doc.parameters[$k] = @{ value = $values[$k] } }
    $file = Join-Path $tmp 'params.json'
    [IO.File]::WriteAllText($file, ($doc | ConvertTo-Json -Depth 10))  # UTF-8 without BOM
    $file
}

$budgetUrl = "https://management.azure.com/subscriptions/$subscriptionId/providers/Microsoft.Consumption/budgets/$($BudgetName)?api-version=2024-08-01"
$budget = az rest --method get --url $budgetUrl 2>$null | ConvertFrom-Json
if ($budget) {
    Write-Host "Budget $BudgetName found (amount $($budget.properties.amount)). Its notifications will be replaced by: actual 25, 50, 80 (kill switch), 90 (kill switch), forecasted 100."
}

$common = @{ location = $Location; alertEmails = $AlertEmail; budgetName = $BudgetName }

if ($WhatIf) {
    az deployment sub what-if --location $Location --template-file $template --parameters "@$(Write-Params $common)"
    Remove-Item -Recurse -Force $tmp
    return
}

# Pass 1: account, runbook shell, role, assignments.
$out = az deployment sub create --name watershed-killswitch-1 --location $Location --template-file $template --parameters "@$(Write-Params $common)" --query properties.outputs -o json | ConvertFrom-Json
if (-not $out) { throw 'Pass 1 failed.' }
$accountId = $out.automationAccountId.value
$runbookId = $out.runbookId.value

# Upload and publish the runbook content.
az rest --method put --url "https://management.azure.com$runbookId/draft/content?api-version=2023-11-01" --headers 'Content-Type=text/powershell' --body "@$runbookFile" | Out-Null
az rest --method post --url "https://management.azure.com$runbookId/publish?api-version=2023-11-01" | Out-Null
Write-Host 'Runbook published.'

# A webhook URI can only be read when it is created: drop any previous webhook, then generate a new URI.
az rest --method delete --url "https://management.azure.com$accountId/webhooks/budget-killswitch?api-version=2015-10-31" 2>$null | Out-Null
$uri = az rest --method post --url "https://management.azure.com$accountId/webhooks/generateUri?api-version=2015-10-31" | ConvertFrom-Json
if (-not $uri) { throw 'Could not generate a webhook URI.' }

# Pass 2: webhook, action group, budget. The URI goes through a parameter file in a temp folder, deleted right after.
$pass2 = $common.Clone()
$pass2.webhookUri = $uri
try {
    az deployment sub create --name watershed-killswitch-2 --location $Location --template-file $template --parameters "@$(Write-Params $pass2)" --query properties.provisioningState -o tsv
}
finally {
    Remove-Item -Recurse -Force $tmp
}
Write-Host 'Kill switch deployed. Test it with the procedure in docs/notes/security.md.'
