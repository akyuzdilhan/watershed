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

@description('Image of the web app already deployed by azd. Empty on the first provision: placeholder.')
param webImage string = ''

@description('Image of the worker already deployed by azd, also used by the replay job. Empty: placeholder.')
param workerImage string = ''

@description('Resource id of the Foundry account, created outside this template. Empty: the worker gets no Foundry role.')
param foundryAccountId string = ''

@description('Foundry project endpoint passed to the worker.')
param foundryProjectEndpoint string = ''

@description('Model deployment and reasoning effort, passed to the worker. No fallback model: the templates are the fallback.')
param foundryModel string = 'gpt-5.4-nano'
param foundryReasoningEffort string = 'medium'

@description('Extra worker arguments, for example --auto-approve in judging mode.')
param workerExtraArgs string = ''

@minValue(0)
@maxValue(1)
@description('Minimum replicas of the worker: 0 scales to zero when idle, 1 keeps one replica up, for example while the demo is judged.')
param workerMinReplicas int = 0

@description('Client id of the Entra app registration for Easy Auth on the web app. Empty: Easy Auth is off.')
param webAuthClientId string = ''

@description('Optional allow-list of producers (comma-separated sign-in names). Empty: any user Easy Auth signs in.')
param webProducers string = ''

@minValue(0)
@maxValue(2)
@description('Minimum replicas of the web app: 0 scales to zero when idle, 1 keeps one warm (for example while the demo is judged).')
param webMinReplicas int = 0

@minValue(1)
@description('Requests per minute allowed to one client IP on the web app.')
param webRateLimitPerMin int = 600

var tags = { 'azd-env-name': environmentName }
var placeholderImage = 'mcr.microsoft.com/azuredocs/containerapps-helloworld:latest'

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
    webImage: empty(webImage) ? placeholderImage : webImage
    workerImage: empty(workerImage) ? placeholderImage : workerImage
    foundryAccountId: foundryAccountId
    foundryProjectEndpoint: foundryProjectEndpoint
    foundryModel: foundryModel
    foundryReasoningEffort: foundryReasoningEffort
    workerExtraArgs: workerExtraArgs
    workerMinReplicas: workerMinReplicas
    webAuthClientId: webAuthClientId
    webProducers: webProducers
    webMinReplicas: webMinReplicas
    webRateLimitPerMin: webRateLimitPerMin
  }
}

output AZURE_RESOURCE_GROUP string = rg.name
output AZURE_CONTAINER_REGISTRY_ENDPOINT string = resources.outputs.registryLoginServer
output AZURE_CONTAINER_REGISTRY_NAME string = resources.outputs.registryName
output SERVICE_WEB_URI string = resources.outputs.webUri
output SERVICE_BUS_FQDN string = resources.outputs.serviceBusFqdn
output SERVICE_BUS_TOPIC string = resources.outputs.serviceBusTopic
output COSMOS_ENDPOINT string = resources.outputs.cosmosEndpoint
output COSMOS_DATABASE string = resources.outputs.cosmosDatabase
output AZURE_WORKER_APP_NAME string = resources.outputs.workerAppName
output AZURE_REPLAY_JOB_NAME string = resources.outputs.replayJobName
