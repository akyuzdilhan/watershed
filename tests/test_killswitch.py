"""The kill switch scripts on fakes: the ARM REST API and the az CLI are replaced by PowerShell functions, so nothing leaves the machine.

The runbook must stop the worker, the replay and the models and leave the web app alone; restore.ps1 must undo exactly that, and never report a restore as finished when a listing failed.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

KILLSWITCH = Path(__file__).parent.parent / "infra" / "killswitch"
POWERSHELL = shutil.which("powershell") or shutil.which("pwsh")  # Windows PowerShell 5.1 is the Automation runtime; pwsh elsewhere

pytestmark = pytest.mark.skipif(POWERSHELL is None, reason="PowerShell is not installed")

ARM_FAKE = r"""
$sub = '/subscriptions/sub'
$appRg = "$sub/resourceGroups/rg-app"
$ns = "$appRg/providers/Microsoft.ServiceBus/namespaces/sb-x"
$topic = "$ns/topics/match-events"
$job = "$appRg/providers/Microsoft.App/jobs/caj-replay-x"
$account = "$sub/resourceGroups/rg-f/providers/Microsoft.CognitiveServices/accounts/acct"
$global:Responses = @{
    "$appRg/providers/Microsoft.App/containerApps" = @"
{"value": [
  {"name": "ca-web-x", "id": "$appRg/providers/Microsoft.App/containerApps/ca-web-x", "tags": {"azd-env-name": "dev", "azd-service-name": "web"}, "properties": {"runningStatus": "Running"}},
  {"name": "ca-worker-x", "id": "$appRg/providers/Microsoft.App/containerApps/ca-worker-x", "tags": {"azd-env-name": "dev", "azd-service-name": "worker"}, "properties": {"runningStatus": "Running"}}
]}
"@
    "$appRg/providers/Microsoft.App/jobs" = "{`"value`": [{`"name`": `"caj-replay-x`", `"id`": `"$job`"}]}"
    "$job/executions" = '{"value": [{"name": "caj-replay-x-old", "properties": {"status": "Succeeded"}}, {"name": "caj-replay-x-now", "properties": {"status": "Running"}}]}'
    "$appRg/providers/Microsoft.ServiceBus/namespaces" = "{`"value`": [{`"name`": `"sb-x`", `"id`": `"$ns`"}]}"
    "$ns/topics" = "{`"value`": [{`"name`": `"match-events`", `"id`": `"$topic`"}]}"
    "$topic/subscriptions" = @"
{"value": [
  {"name": "worker", "id": "$topic/subscriptions/worker", "properties": {"status": "Active", "requiresSession": true, "lockDuration": "PT1M", "messageCount": 3}},
  {"name": "other", "id": "$topic/subscriptions/other", "properties": {"status": "Active"}}
]}
"@
    "$account/deployments" = @"
{"value": [{"name": "model-a", "id": "$account/deployments/model-a", "sku": {"name": "GlobalStandard", "capacity": 20},
            "properties": {"model": {"format": "OpenAI", "name": "model-a", "version": "1"}, "raiPolicyName": "Microsoft.DefaultV2", "versionUpgradeOption": "OnceCurrentVersionExpired"}}]}
"@
}
$global:Calls = New-Object System.Collections.ArrayList
$global:FailOn = $null

function Invoke-RestMethod {
    param([string] $Method, [string] $Uri, $Headers, [string] $ContentType, [switch] $UseBasicParsing, $Body)
    [void]$global:Calls.Add("$($Method.ToUpper()) $Uri $Body")
    if ($Uri -match '\?resource=') { return [pscustomobject]@{ access_token = 'token' } }
    $path = ($Uri -replace '^https://management\.azure\.com', '') -replace '\?.*$', ''
    if ($global:FailOn -and $path -match $global:FailOn) { throw "403 Forbidden on $path" }
    if ($Method -ne 'GET') { return $null }
    if ($global:Responses.ContainsKey($path)) { return ($global:Responses[$path] | ConvertFrom-Json) }
    return ('{"value": []}' | ConvertFrom-Json)
}

function Run-Scenario([string] $Name, [string] $Mode, [string] $FailOn) {
    $global:Calls.Clear()
    $global:FailOn = $FailOn
    Write-Output "SCENARIO $Name"
    try { & $runbook -SubscriptionId sub -AppResourceGroup rg-app -FoundryResourceGroup rg-f -FoundryAccount acct -Mode $Mode | ForEach-Object { "OUT $_" } }
    catch { "ERROR $($_.Exception.Message)" }
    foreach ($c in $global:Calls) { "CALL $c" }
}

Run-Scenario dry DryRun $null
Run-Scenario kill Kill $null
Run-Scenario jobs-denied Kill '/jobs$'
"""

AZ_FAKE = r"""
$global:AzCalls = New-Object System.Collections.ArrayList
$global:AzFail = $null
$apps = '[{"name": "ca-web-x", "id": "/subscriptions/sub/resourceGroups/rg-app/providers/Microsoft.App/containerApps/ca-web-x", "tags": {"azd-service-name": "web"}},' +
        ' {"name": "ca-worker-x", "id": "/subscriptions/sub/resourceGroups/rg-app/providers/Microsoft.App/containerApps/ca-worker-x", "tags": {"azd-service-name": "worker"}}]'

function az {
    $line = $args -join ' '
    [void]$global:AzCalls.Add($line)
    $global:LASTEXITCODE = 0
    if ($global:AzFail -and $line -match $global:AzFail) { $global:LASTEXITCODE = 1; return }
    switch -regex ($line) {
        '^account show' { return 'sub' }
        '^servicebus namespace list' { return 'sb-x' }
        '^servicebus topic list' { return 'match-events' }
        '^servicebus topic subscription list' { return 'Disabled' }
        '^resource list' { return $apps }
        default { return }
    }
}

function Run-Scenario([string] $Name, [string] $AzFail, [switch] $DryRun) {
    $global:AzCalls.Clear()
    $global:AzFail = $AzFail
    Write-Output "SCENARIO $Name"
    try { & $restore -AppResourceGroup rg-app -FoundryResourceGroup rg-f -FoundryAccount acct -DryRun:$DryRun 6>&1 | ForEach-Object { "OUT $_" } }
    catch { "ERROR $($_.Exception.Message)" }
    foreach ($c in $global:AzCalls) { "CALL $c" }
}

Run-Scenario dry $null -DryRun
Run-Scenario restore $null
Run-Scenario list-fails '^resource list'
"""


def run(tmp_path: Path, script: str, body: str, variable: str) -> dict[str, dict[str, list[str]]]:
    harness = tmp_path / "harness.ps1"
    harness.write_text(f"$ErrorActionPreference = 'Stop'\n${variable} = '{KILLSWITCH / script}'\n{body}", encoding="ascii")
    done = subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(harness)],
                          capture_output=True, text=True, timeout=180)
    assert done.returncode == 0, done.stderr
    scenarios: dict[str, dict[str, list[str]]] = {}
    current = None
    for line in done.stdout.splitlines():
        kind, _, rest = line.partition(" ")
        if kind == "SCENARIO":
            current = scenarios.setdefault(rest, {"OUT": [], "CALL": [], "ERROR": []})
        elif current is not None and kind in current:
            current[kind].append(rest)
    return scenarios


@pytest.fixture(scope="module")
def runbook(tmp_path_factory):
    return run(tmp_path_factory.mktemp("runbook"), "Stop-Watershed.ps1", ARM_FAKE, "runbook")


@pytest.fixture(scope="module")
def restore(tmp_path_factory):
    return run(tmp_path_factory.mktemp("restore"), "restore.ps1", AZ_FAKE, "restore")


def changes(calls: list[str]) -> list[str]:
    return [c for c in calls if not c.startswith("GET ")]


def test_the_dry_run_lists_what_would_stop_and_changes_nothing(runbook):
    dry = runbook["dry"]
    assert not changes(dry["CALL"]) and not dry["ERROR"]
    out = "\n".join(dry["OUT"])
    for line in ("KEEP   container app ca-web-x", "WOULD  stop container app ca-worker-x", "WOULD  stop execution caj-replay-x-now of job caj-replay-x",
                 "KEEP   subscription match-events/other", "WOULD  disable subscription match-events/worker", "WOULD  delete Foundry deployment model-a",
                 "Kill switch finished (DryRun)."):
        assert line in out
    assert "caj-replay-x-old" not in out, "a finished execution is not stopped again"
    assert any(o.startswith("RESTORE-SETTINGS ") and '"model-a"' in o for o in dry["OUT"])


def test_kill_stops_the_worker_the_replay_and_the_models_and_spares_the_web_app(runbook):
    kill = runbook["kill"]
    assert not kill["ERROR"], kill["ERROR"]
    done = changes(kill["CALL"])
    assert not [c for c in done if "ca-web-x" in c or "subscriptions/other" in c], "the web app and the other subscriptions are never touched"
    assert any(c.startswith("POST ") and "/containerApps/ca-worker-x/stop?" in c for c in done)
    assert any(c.startswith("POST ") and "/jobs/caj-replay-x/executions/caj-replay-x-now/stop?" in c for c in done)
    assert not any("caj-replay-x-old" in c for c in done)
    put = [c for c in done if c.startswith("PUT ")]
    assert len(put) == 1 and "/subscriptions/worker?" in put[0] and '"status":"Disabled"' in put[0] and '"requiresSession":true' in put[0]
    assert "messageCount" not in put[0], "read-only properties are not sent back"
    assert any(c.startswith("DELETE ") and "/deployments/model-a?" in c for c in done)
    assert "Kill switch finished (Kill)." in kill["OUT"]


def test_a_part_that_cannot_list_its_resources_does_not_stop_the_others(runbook):
    denied = runbook["jobs-denied"]
    done = changes(denied["CALL"])
    assert any("/containerApps/ca-worker-x/stop?" in c for c in done)
    assert any(c.startswith("DELETE ") and "/deployments/model-a?" in c for c in done), "the models are still deleted"
    assert any(o.startswith("FAILED list container apps jobs") for o in denied["OUT"])
    assert denied["ERROR"] and "Kill switch incomplete" in denied["ERROR"][0], "the job ends as failed"


def test_restore_starts_the_worker_only_and_enables_its_subscription(restore):
    dry = restore["dry"]
    assert not dry["ERROR"] and not [c for c in dry["CALL"] if c.startswith(("rest ", "servicebus topic subscription update"))]
    assert "KEEP   container app ca-web-x" in dry["OUT"] and "WOULD  start container app ca-worker-x" in dry["OUT"]
    assert "Dry run finished: nothing was changed." in dry["OUT"]
    done = restore["restore"]
    assert not done["ERROR"], done["ERROR"]
    starts = [c for c in done["CALL"] if c.startswith("rest --method post")]
    assert len(starts) == 1 and "/containerApps/ca-worker-x/start?" in starts[0]
    assert any(c.startswith("servicebus topic subscription update") and "-n worker" in c and "--status Active" in c for c in done["CALL"])
    assert sum(c.startswith("rest --method put") for c in done["CALL"]) == 2, "both default Foundry deployments are recreated"
    assert any(o.startswith("Restore finished.") for o in done["OUT"])


def test_restore_does_not_report_success_when_a_listing_fails(restore):
    failed = restore["list-fails"]
    assert failed["ERROR"] and "Could not list container apps" in failed["ERROR"][0]
    assert not any(o.startswith("Restore finished") for o in failed["OUT"])
    assert not any(c.startswith("rest --method post") for c in failed["CALL"])
