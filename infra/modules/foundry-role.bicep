// Grants a principal a role on the Foundry account, which lives in its own resource group outside the azd template.
// Deployed with scope set to the Foundry resource group.

@description('Name of the existing Foundry (AIServices) account.')
param accountName string

@description('Object id of the principal (a managed identity).')
param principalId string

@description('Role definition id (GUID) to grant on the account.')
param roleDefinitionId string

resource account 'Microsoft.CognitiveServices/accounts@2024-10-01' existing = {
  name: accountName
}

resource assignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(account.id, principalId, roleDefinitionId)
  scope: account
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleDefinitionId)
    principalId: principalId
    principalType: 'ServicePrincipal'
  }
}
