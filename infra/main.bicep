targetScope = 'subscription'

@minLength(1)
@maxLength(64)
@description('azd environment name, used to name the resource group and tag resources.')
param environmentName string

@minLength(1)
@description('Region for all resources.')
param location string

@description('Object id of the developer running azd (set by azd), granted data access for local runs.')
param principalId string = ''

var tags = { 'azd-env-name': environmentName }

resource rg 'Microsoft.Resources/resourceGroups@2024-03-01' = {
  name: 'rg-${environmentName}'
  location: location
  tags: tags
}

module resources 'resources.bicep' = {
  name: 'resources'
  scope: rg
  params: {
    location: location
    tags: tags
    principalId: principalId
  }
}

output AZURE_CONTAINER_REGISTRY_ENDPOINT string = resources.outputs.registryLoginServer
output AZURE_CONTAINER_REGISTRY_NAME string = resources.outputs.registryName
output SERVICE_WEB_URI string = resources.outputs.webUri
output SERVICE_BUS_FQDN string = resources.outputs.serviceBusFqdn
output SERVICE_BUS_TOPIC string = resources.outputs.serviceBusTopic
output COSMOS_ENDPOINT string = resources.outputs.cosmosEndpoint
output COSMOS_DATABASE string = resources.outputs.cosmosDatabase
