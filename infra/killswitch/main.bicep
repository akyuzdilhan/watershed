// Watershed kill switch. Deployed on its own with deploy.ps1, never by azd: infra/main.bicep does not reference this file.
// Budget alert at 80 % (and 90 %) actual -> action group -> Automation webhook -> runbook Stop-Watershed (Mode=Kill).
targetScope = 'subscription'

@description('Region of the Automation account.')
param location string = 'swedencentral'

@description('Resource group created for the kill switch, kept apart so that azd down or azd up never touches it.')
param killSwitchResourceGroup string = 'rg-watershed-killswitch'

@description('Resource group of the app (container apps, Service Bus).')
param appResourceGroup string = 'rg-watershed-dev'

@description('Resource group and name of the Foundry account whose deployments are deleted.')
param foundryResourceGroup string = 'rg-watershed-foundry'
param foundryAccount string = 'watershed-foundry-da'

@description('Automation webhook URI, from deploy.ps1 (generateUri). Empty on the first pass: the webhook, action group and budget are skipped.')
@secure()
param webhookUri string = ''

@description('Webhook expiry. One year by default.')
param webhookExpiry string = dateTimeAdd(utcNow(), 'P1Y')

@description('Subscription budget redeclared with the kill switch wired in. Kept identical to the existing hackathon-budget except for one notification.')
param budgetName string = 'hackathon-budget'
param budgetAmount int = 50
param budgetStartDate string = '2026-10-01T00:00:00Z'
param budgetEndDate string = '2028-09-30T00:00:00Z'

@description('Who gets the budget e-mails. Passed at deploy time so that no address is committed.')
param alertEmails array

var tags = { purpose: 'watershed-killswitch' }
var wireBudget = !empty(webhookUri)
var appRgId = '${subscription().id}/resourceGroups/${appResourceGroup}'
var foundryRgId = '${subscription().id}/resourceGroups/${foundryResourceGroup}'

resource rg 'Microsoft.Resources/resourceGroups@2024-03-01' = {
  name: killSwitchResourceGroup
  location: location
  tags: tags
}

// The narrowest set of actions the runbook needs, assignable only on the two resource groups.
resource killSwitchRole 'Microsoft.Authorization/roleDefinitions@2022-04-01' = {
  name: guid(subscription().id, 'watershed-killswitch-operator')
  properties: {
    roleName: 'Watershed kill switch operator'
    description: 'Stop container apps, disable Service Bus subscriptions and delete Foundry deployments. Used by the kill switch runbook.'
    type: 'customRole'
    permissions: [
      {
        actions: [
          'Microsoft.App/containerApps/read'
          'Microsoft.App/containerApps/stop/action'
          'Microsoft.ServiceBus/namespaces/read'
          'Microsoft.ServiceBus/namespaces/topics/read'
          'Microsoft.ServiceBus/namespaces/topics/subscriptions/read'
          'Microsoft.ServiceBus/namespaces/topics/subscriptions/write'
          'Microsoft.CognitiveServices/accounts/deployments/read'
          'Microsoft.CognitiveServices/accounts/deployments/delete'
        ]
        notActions: []
      }
    ]
    assignableScopes: [ appRgId, foundryRgId ]
  }
}

module automation 'automation.bicep' = {
  name: 'killswitch-automation'
  scope: rg
  params: {
    location: location
    tags: tags
    webhookUri: webhookUri
    webhookExpiry: webhookExpiry
    runbookParameters: {
      SubscriptionId: subscription().subscriptionId
      AppResourceGroup: appResourceGroup
      FoundryResourceGroup: foundryResourceGroup
      FoundryAccount: foundryAccount
      Mode: 'Kill'
    }
  }
}

module appAssignment 'assignment.bicep' = {
  name: 'killswitch-role-app'
  scope: resourceGroup(appResourceGroup)
  params: {
    principalId: automation.outputs.principalId
    roleDefinitionId: killSwitchRole.id
  }
}

module foundryAssignment 'assignment.bicep' = {
  name: 'killswitch-role-foundry'
  scope: resourceGroup(foundryResourceGroup)
  params: {
    principalId: automation.outputs.principalId
    roleDefinitionId: killSwitchRole.id
  }
}

// A budget holds at most five notifications. The existing forecasted 50 % one is replaced by the 80 % actual kill switch;
// 90 % actual also fires the kill switch, in case the app was restored and the cost keeps climbing in the same month.
resource budget 'Microsoft.Consumption/budgets@2024-08-01' = if (wireBudget) {
  name: budgetName
  properties: {
    category: 'Cost'
    amount: budgetAmount
    timeGrain: 'Monthly'
    timePeriod: {
      startDate: budgetStartDate
      endDate: budgetEndDate
    }
    notifications: {
      actual_GreaterThan_25_Percent: {
        enabled: true
        operator: 'GreaterThan'
        threshold: 25
        thresholdType: 'Actual'
        contactEmails: alertEmails
      }
      actual_GreaterThan_50_Percent: {
        enabled: true
        operator: 'GreaterThan'
        threshold: 50
        thresholdType: 'Actual'
        contactEmails: alertEmails
      }
      actual_GreaterThan_80_Percent_KillSwitch: {
        enabled: true
        operator: 'GreaterThan'
        threshold: 80
        thresholdType: 'Actual'
        contactEmails: alertEmails
        contactGroups: [ automation.outputs.actionGroupId ]
      }
      actual_GreaterThan_90_Percent: {
        enabled: true
        operator: 'GreaterThan'
        threshold: 90
        thresholdType: 'Actual'
        contactEmails: alertEmails
        contactGroups: [ automation.outputs.actionGroupId ]
      }
      forecasted_GreaterThan_100_Percent: {
        enabled: true
        operator: 'GreaterThan'
        threshold: 100
        thresholdType: 'Forecasted'
        contactEmails: alertEmails
      }
    }
  }
}

output automationAccountId string = automation.outputs.automationAccountId
output runbookId string = automation.outputs.runbookId
output principalId string = automation.outputs.principalId
output actionGroupId string = automation.outputs.actionGroupId
