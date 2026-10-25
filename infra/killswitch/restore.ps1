<#
.SYNOPSIS
Undoes the Watershed kill switch: recreates the Foundry deployments, re-enables the worker subscription, starts the worker container app.

.DESCRIPTION
Run by hand, signed in with `az login` (Contributor on the two resource groups is enough).
The kill switch leaves the web app running, so there is nothing to restore there. A replay it stopped is not resumed: start a new run of the job when needed.
The default deployments match the settings the runbook prints as RESTORE-SETTINGS in its job output; check them there if they changed.
-DryRun prints what would be done. If a listing fails, the script stops with an error and does not report the restore as finished.

.EXAMPLE
./infra/killswitch/restore.ps1 -DryRun
./infra/killswitch/restore.ps1
#>
param(
    [string] $AppResourceGroup = 'rg-watershed-dev',
    [string] $FoundryResourceGroup = 'rg-watershed-foundry',
    [string] $FoundryAccount = 'watershed-foundry-da',
    [object[]] $Deployments = @(
        @{ name = 'gpt-5.4-nano'; model = 'gpt-5.4-nano'; version = '2026-03-17'; sku = 'GlobalStandard'; capacity = 20 },
        @{ name = 'gpt-5-mini'; model = 'gpt-5-mini'; version = '2025-08-07'; sku = 'GlobalStandard'; capacity = 20 }
    ),
    [switch] $DryRun
)

$ErrorActionPreference = 'Stop'
# Names from infra/resources.bicep, as in Stop-Watershed.ps1: the worker app's azd tag and its subscription.
$workerService = 'worker'
$workerSubscription = 'worker'
$subscriptionId = az account show --query id -o tsv
if ($LASTEXITCODE -ne 0 -or -not $subscriptionId) { throw 'Run az login first.' }
$arm = 'https://management.azure.com'
$tmp = New-Item -ItemType Directory -Force -Path (Join-Path ([IO.Path]::GetTempPath()) "ws-restore-$PID")

function Step([string] $Description, [scriptblock] $Action) {
    if ($DryRun) { Write-Host "WOULD  $Description"; return }
    & $Action
    if ($LASTEXITCODE -ne 0) { throw "FAILED $Description" }
    Write-Host "DONE   $Description"
}

# The output of an az listing; a failed call stops the script instead of looking like an empty list.
function Get-AzOutput([string] $What, [scriptblock] $Command) {
    $out = & $Command
    if ($LASTEXITCODE -ne 0) { throw "Could not list $What (az exit code $LASTEXITCODE). Restore not finished." }
    @($out | Where-Object { $_ })
}

try {
    # 1. Foundry deployments, recreated with the settings they had (PUT is idempotent if one still exists).
    $account = "/subscriptions/$subscriptionId/resourceGroups/$FoundryResourceGroup/providers/Microsoft.CognitiveServices/accounts/$FoundryAccount"
    foreach ($d in $Deployments) {
        $body = @{
            sku = @{ name = $d.sku; capacity = $d.capacity }
            properties = @{
                model = @{ format = 'OpenAI'; name = $d.model; version = $d.version }
                raiPolicyName = 'Microsoft.DefaultV2'
                versionUpgradeOption = 'OnceCurrentVersionExpired'
            }
        }
        $file = Join-Path $tmp "$($d.name).json"
        [IO.File]::WriteAllText($file, ($body | ConvertTo-Json -Depth 5))
        Step "recreate Foundry deployment $($d.name) ($($d.model) $($d.version), $($d.sku) $($d.capacity))" {
            az rest --method put --url "$arm$account/deployments/$($d.name)?api-version=2024-10-01" --body "@$file" | Out-Null
        }
    }

    # 2. The worker subscription of each topic back to Active (the kill switch disables no other).
    foreach ($ns in (Get-AzOutput "Service Bus namespaces in $AppResourceGroup" { az servicebus namespace list -g $AppResourceGroup --query "[].name" -o tsv })) {
        foreach ($topic in (Get-AzOutput "topics of $ns" { az servicebus topic list -g $AppResourceGroup --namespace-name $ns --query "[].name" -o tsv })) {
            $status = Get-AzOutput "subscriptions of $ns/$topic" { az servicebus topic subscription list -g $AppResourceGroup --namespace-name $ns --topic-name $topic --query "[?name=='$workerSubscription'].status" -o tsv }
            if ($status.Count -eq 0) { continue }
            if ($status[0] -eq 'Active') { Write-Host "SKIP   subscription $ns/$topic/$workerSubscription already active"; continue }
            Step "enable subscription $ns/$topic/$workerSubscription" {
                az servicebus topic subscription update -g $AppResourceGroup --namespace-name $ns --topic-name $topic -n $workerSubscription --status Active | Out-Null
            }
        }
    }

    # 3. The worker container app: start (ingress and revisions come back as they were before the stop). The web app is not touched.
    $apps = (Get-AzOutput "container apps in $AppResourceGroup" { az resource list -g $AppResourceGroup --resource-type Microsoft.App/containerApps -o json }) -join "`n" | ConvertFrom-Json
    foreach ($app in $apps) {
        if ($app.tags.'azd-service-name' -ne $workerService) { Write-Host "KEEP   container app $($app.name)"; continue }
        $id = $app.id
        Step "start container app $($app.name)" {
            az rest --method post --url "$arm$id/start?api-version=2024-03-01" | Out-Null
        }
    }
}
finally {
    Remove-Item -Recurse -Force $tmp
}
if ($DryRun) { Write-Host 'Dry run finished: nothing was changed.' }
else { Write-Host 'Restore finished. Check: az containerapp show -g' $AppResourceGroup '-n <worker app> --query properties.runningStatus' }
