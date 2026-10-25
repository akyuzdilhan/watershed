// Automation account, runbook, webhook and action group of the kill switch.
param location string
param tags object

@secure()
param webhookUri string
param webhookExpiry string
param runbookParameters object

var suffix = uniqueString(resourceGroup().id)
var runbookName = 'Stop-Watershed'
var withWebhook = !empty(webhookUri)

resource account 'Microsoft.Automation/automationAccounts@2023-11-01' = {
  name: 'aa-killswitch-${suffix}'
  location: location
  tags: tags
  identity: { type: 'SystemAssigned' }
  properties: {
    sku: { name: 'Basic' }
    publicNetworkAccess: true
    // Local auth stays on: Automation webhooks do not work when it is disabled.
    disableLocalAuth: false
  }
}

// The content is uploaded and published by deploy.ps1 (an ARM template can only link to content by URL).
resource runbook 'Microsoft.Automation/automationAccounts/runbooks@2023-11-01' = {
  parent: account
  name: runbookName
  location: location
  tags: tags
  properties: {
    runbookType: 'PowerShell'
    description: 'Watershed kill switch: stop the worker app and the replay job, disable the worker subscription, delete Foundry deployments. The web app keeps running.'
    logProgress: false
    logVerbose: false
  }
}

resource webhook 'Microsoft.Automation/automationAccounts/webhooks@2015-10-31' = if (withWebhook) {
  parent: account
  name: 'budget-killswitch'
  properties: {
    isEnabled: true
    uri: webhookUri
    expiryTime: webhookExpiry
    runbook: { name: runbook.name }
    parameters: runbookParameters
  }
}

resource actionGroup 'Microsoft.Insights/actionGroups@2023-01-01' = if (withWebhook) {
  name: 'ag-killswitch-${suffix}'
  location: 'global'
  tags: tags
  properties: {
    groupShortName: 'wskillsw'
    enabled: true
    automationRunbookReceivers: [
      {
        name: 'stop-watershed'
        automationAccountId: account.id
        runbookName: runbook.name
        webhookResourceId: webhook.id
        isGlobalRunbook: false
        serviceUri: webhookUri
        useCommonAlertSchema: false
      }
    ]
  }
}

output automationAccountId string = account.id
output runbookId string = runbook.id
output principalId string = account.identity.principalId
output actionGroupId string = withWebhook ? actionGroup.id : ''
