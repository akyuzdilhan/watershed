<#
.SYNOPSIS
Starts the kill switch runbook by hand and prints its output. DryRun by default; -Mode Kill for a real stop (then run restore.ps1).
#>
param(
    [ValidateSet('DryRun', 'Kill')] [string] $Mode = 'DryRun',
    [string] $KillSwitchResourceGroup = 'rg-watershed-killswitch',
    [string] $AppResourceGroup = 'rg-watershed-dev',
    [string] $FoundryResourceGroup = 'rg-watershed-foundry',
    [string] $FoundryAccount = 'watershed-foundry-da'
)

$ErrorActionPreference = 'Stop'
$subscriptionId = az account show --query id -o tsv
if (-not $subscriptionId) { throw 'Run az login first.' }
$accountId = az resource list -g $KillSwitchResourceGroup --resource-type Microsoft.Automation/automationAccounts --query "[0].id" -o tsv
if (-not $accountId) { throw "No Automation account in ${KillSwitchResourceGroup}: run deploy.ps1 first." }

$job = "https://management.azure.com$accountId/jobs/$([guid]::NewGuid())"
$api = 'api-version=2023-11-01'
$body = @{ properties = @{ runbook = @{ name = 'Stop-Watershed' }; parameters = @{
    SubscriptionId = $subscriptionId; AppResourceGroup = $AppResourceGroup; FoundryResourceGroup = $FoundryResourceGroup; FoundryAccount = $FoundryAccount; Mode = $Mode } } }
$file = Join-Path ([IO.Path]::GetTempPath()) "ws-job-$PID.json"
[IO.File]::WriteAllText($file, ($body | ConvertTo-Json -Depth 5))
try { az rest --method put --url "$($job)?$api" --body "@$file" | Out-Null }
finally { Remove-Item -Force $file }
Write-Host "Job started ($Mode). Waiting for it to finish..."

do {
    Start-Sleep -Seconds 10
    $status = az rest --method get --url "$($job)?$api" --query properties.status -o tsv
    Write-Host "  $status"
} while ($status -notin 'Completed', 'Failed', 'Stopped', 'Suspended')

az rest --method get --url "$job/output?$api"
