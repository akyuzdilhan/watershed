targetScope = 'subscription'

@minLength(1)
@maxLength(64)
@description('azd environment name, used to name the resource group and tag resources.')
param environmentName string

@minLength(1)
@description('Region for all resources.')
param location string

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
  }
}

output AZURE_CONTAINER_REGISTRY_ENDPOINT string = resources.outputs.registryLoginServer
output AZURE_CONTAINER_REGISTRY_NAME string = resources.outputs.registryName
output SERVICE_WEB_URI string = resources.outputs.webUri
