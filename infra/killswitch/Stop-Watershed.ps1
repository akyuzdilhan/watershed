<#
.SYNOPSIS
Watershed kill switch, run by Azure Automation (Windows PowerShell 5.1 runbook).

.DESCRIPTION
Started by the budget action group through a webhook (Mode=Kill is set on the webhook) or by hand (Mode defaults to DryRun).
In Kill mode it:
  1. stops every container app in the app resource group (Container Apps "Stop": all revisions deactivated, no replica, no ingress);
  2. disables every Service Bus topic subscription in the app resource group (status Disabled: nothing is delivered to the worker);
  3. prints the settings of every model deployment of the Foundry account, then deletes them (a GlobalStandard deployment cannot be scaled to 0).
restore.ps1 undoes all three. The runbook only uses the Automation account's system-assigned identity and the ARM REST API, no Az module.
#>
param(
    [object] $WebhookData,
    [string] $SubscriptionId,
    [string] $AppResourceGroup,
    [string] $FoundryResourceGroup,
    [string] $FoundryAccount,
    [ValidateSet('DryRun', 'Kill')]
    [string] $Mode = 'DryRun'
)

$ErrorActionPreference = 'Stop'
$arm = 'https://management.azure.com'
$apiApps = '2024-03-01'
$apiBus = '2024-01-01'
$apiFoundry = '2024-10-01'

foreach ($name in 'SubscriptionId', 'AppResourceGroup', 'FoundryResourceGroup', 'FoundryAccount') {
    if (-not (Get-Variable -Name $name -ValueOnly)) { throw "Missing parameter $name" }
}

if ($WebhookData) {
    # The budget alert payload, for the job history. It carries no secret.
    $body = [string]$WebhookData.RequestBody
    Write-Output ("Started by webhook {0}: {1}" -f $WebhookData.WebhookName, $body.Substring(0, [Math]::Min(2000, $body.Length)))
}
Write-Output "Mode: $Mode"

# Token for the system-assigned identity (Automation's managed identity endpoint).
$tokenResponse = Invoke-RestMethod -Method Get -UseBasicParsing `
    -Uri "$($env:IDENTITY_ENDPOINT)?resource=$arm/" `
    -Headers @{ 'X-IDENTITY-HEADER' = $env:IDENTITY_HEADER; 'Metadata' = 'True' }
$headers = @{ Authorization = "Bearer $($tokenResponse.access_token)" }

function Invoke-Arm([string] $Method, [string] $Path, $Body = $null) {
    $uri = if ($Path.StartsWith('https://')) { $Path } else { "$arm$Path" }
    $params = @{ Method = $Method; Uri = $uri; Headers = $headers; ContentType = 'application/json'; UseBasicParsing = $true }
    if ($null -ne $Body) { $params.Body = ($Body | ConvertTo-Json -Depth 20 -Compress) }
    Invoke-RestMethod @params
}

function Get-ArmList([string] $Path) {
    $items = @()
    $next = $Path
    while ($next) {
        $page = Invoke-Arm GET $next
        if ($page.value) { $items += $page.value }
        $next = $page.nextLink
    }
    $items
}

function Invoke-Step([string] $Description, [scriptblock] $Action) {
    if ($Mode -eq 'Kill') {
        try { & $Action; Write-Output "DONE   $Description" }
        catch { $script:failures += "$Description : $($_.Exception.Message)"; Write-Output "FAILED $Description : $($_.Exception.Message)" }
    }
    else { Write-Output "WOULD  $Description" }
}

$failures = @()
$appRg = "/subscriptions/$SubscriptionId/resourceGroups/$AppResourceGroup"

# 1. Container apps: stop (web, and the worker if it runs as a container app).
foreach ($app in Get-ArmList "$appRg/providers/Microsoft.App/containerApps?api-version=$apiApps") {
    $id = $app.id
    Invoke-Step "stop container app $($app.name)" { Invoke-Arm POST "$id/stop?api-version=$apiApps" | Out-Null }
}

# 2. Service Bus: disable every topic subscription. PUT replaces the whole resource, so the current properties are sent back with only the status changed.
$readOnly = 'messageCount', 'createdAt', 'updatedAt', 'accessedAt', 'countDetails'
foreach ($ns in Get-ArmList "$appRg/providers/Microsoft.ServiceBus/namespaces?api-version=$apiBus") {
    foreach ($topic in Get-ArmList "$($ns.id)/topics?api-version=$apiBus") {
        foreach ($sub in Get-ArmList "$($topic.id)/subscriptions?api-version=$apiBus") {
            if ($sub.properties.status -eq 'Disabled') { Write-Output "SKIP   subscription $($topic.name)/$($sub.name) already disabled"; continue }
            $props = @{}
            foreach ($p in $sub.properties.PSObject.Properties) { if ($readOnly -notcontains $p.Name) { $props[$p.Name] = $p.Value } }
            $props.status = 'Disabled'
            $subId = $sub.id
            Invoke-Step "disable subscription $($topic.name)/$($sub.name)" { Invoke-Arm PUT "$($subId)?api-version=$apiBus" @{ properties = $props } | Out-Null }
        }
    }
}

# 3. Foundry: record each deployment, then delete it.
$account = "/subscriptions/$SubscriptionId/resourceGroups/$FoundryResourceGroup/providers/Microsoft.CognitiveServices/accounts/$FoundryAccount"
foreach ($dep in Get-ArmList "$account/deployments?api-version=$apiFoundry") {
    $snapshot = [ordered]@{
        name = $dep.name
        sku = @{ name = $dep.sku.name; capacity = $dep.sku.capacity }
        model = @{ format = $dep.properties.model.format; name = $dep.properties.model.name; version = $dep.properties.model.version }
        raiPolicyName = $dep.properties.raiPolicyName
        versionUpgradeOption = $dep.properties.versionUpgradeOption
    }
    Write-Output ("RESTORE-SETTINGS " + ($snapshot | ConvertTo-Json -Depth 5 -Compress))
    $depId = $dep.id
    Invoke-Step "delete Foundry deployment $($dep.name)" { Invoke-Arm DELETE "$($depId)?api-version=$apiFoundry" | Out-Null }
}

if ($failures.Count -gt 0) { throw ("Kill switch incomplete: " + ($failures -join ' | ')) }
Write-Output "Kill switch finished ($Mode)."
