<#
.SYNOPSIS
Undoes the Watershed kill switch: recreates the Foundry deployments, re-enables the Service Bus subscriptions, starts the container apps.

.DESCRIPTION
Run by a human with `az login` (Contributor on the two resource groups is enough).
The default deployments match the settings the runbook prints as RESTORE-SETTINGS in its job output; check them there if they changed.
-DryRun prints what would be done.

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
$subscriptionId = az account show --query id -o tsv
if (-not $subscriptionId) { throw 'Run az login first.' }
$arm = 'https://management.azure.com'
$tmp = New-Item -ItemType Directory -Force -Path (Join-Path ([IO.Path]::GetTempPath()) "ws-restore-$PID")

function Step([string] $Description, [scriptblock] $Action) {
    if ($DryRun) { Write-Host "WOULD  $Description"; return }
    & $Action
    if ($LASTEXITCODE -ne 0) { throw "FAILED $Description" }
    Write-Host "DONE   $Description"
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

    # 2. Service Bus subscriptions back to Active.
    foreach ($ns in (az servicebus namespace list -g $AppResourceGroup --query "[].name" -o tsv)) {
        foreach ($topic in (az servicebus topic list -g $AppResourceGroup --namespace-name $ns --query "[].name" -o tsv)) {
            foreach ($sub in (az servicebus topic subscription list -g $AppResourceGroup --namespace-name $ns --topic-name $topic --query "[?status!='Active'].name" -o tsv)) {
                Step "enable subscription $ns/$topic/$sub" {
                    az servicebus topic subscription update -g $AppResourceGroup --namespace-name $ns --topic-name $topic -n $sub --status Active | Out-Null
                }
            }
        }
    }

    # 3. Container apps: start (ingress and revisions come back as they were before the stop).
    foreach ($id in (az containerapp list -g $AppResourceGroup --query "[].id" -o tsv)) {
        Step "start container app $(Split-Path -Leaf $id)" {
            az rest --method post --url "$arm$id/start?api-version=2024-03-01" | Out-Null
        }
    }
}
finally {
    Remove-Item -Recurse -Force $tmp
}
Write-Host 'Restore finished. Check: az containerapp show -g' $AppResourceGroup '-n <app> --query properties.runningStatus'
