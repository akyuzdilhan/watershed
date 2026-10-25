param location string
param tags object

@description('Object id of the developer running azd, granted data access for local runs. Empty to skip.')
param principalId string = ''

@description('Cosmos DB free tier (1000 RU/s, 25 GB). Only one account per subscription can use it.')
param cosmosFreeTier bool = true

@description('Image of the web app. azd writes SERVICE_WEB_IMAGE_NAME after the first deploy, so a later provision keeps it instead of going back to the placeholder.')
param webImage string = 'mcr.microsoft.com/azuredocs/containerapps-helloworld:latest'

@description('Image of the worker, also used by the replay job (SERVICE_WORKER_IMAGE_NAME after the first deploy).')
param workerImage string = 'mcr.microsoft.com/azuredocs/containerapps-helloworld:latest'

@description('Resource id of the Foundry account (created outside this template). Empty: no Foundry role is granted.')
param foundryAccountId string = ''

@description('Foundry project endpoint, model deployments and reasoning effort, passed to the worker.')
param foundryProjectEndpoint string = ''

param foundryModel string = 'gpt-5.4-nano'
param foundryModelFallback string = 'gpt-5-mini'
param foundryReasoningEffort string = 'medium'

@description('Role granted to the worker on the Foundry account. Default: Foundry User (formerly Azure AI User), the role Microsoft documents for calling models through a Foundry project.')
param foundryRoleId string = '53ca6127-db72-4b80-b1b0-d745d6d5456d'

@description('Extra arguments for the worker, for example --auto-approve in judging mode.')
param workerExtraArgs string = ''

@description('Client id of the Entra app registration for Easy Auth on the web app. Empty: Easy Auth is off.')
param webAuthClientId string = ''

@description('Optional allow-list of producers (comma-separated sign-in names) on the web app. Empty: any user Easy Auth signs in.')
param webProducers string = ''

@minValue(0)
@maxValue(2)
@description('Minimum replicas of the web app: 0 scales to zero when nobody visits, 1 keeps one replica warm (no cold start).')
param webMinReplicas int = 0

@minValue(1)
@description('Requests per minute allowed to one client IP on the web app pages and callbacks.')
param webRateLimitPerMin int = 600

var suffix = uniqueString(resourceGroup().id)
var acrPullRoleId = '7f951dda-4ed3-4680-a7ca-43fe172d538d'
var serviceBusSenderRoleId = '69a216fc-b8fb-44d8-bc22-1f3c2cd27a39'
var serviceBusReceiverRoleId = '4f6d3b9b-027b-4f4c-9142-0e5a2a2247e0'
// Built-in Cosmos DB data-plane role: Cosmos DB Built-in Data Contributor
var cosmosDataContributorRoleId = '00000000-0000-0000-0000-000000000002'
var topicName = 'match-events'
var workerSubscriptionName = 'worker'
var cosmosDatabaseName = 'watershed'
var cosmosContainers = [ 'events', 'cards', 'audit' ]
var serviceBusFqdn = '${serviceBus.name}.servicebus.windows.net'
var easyAuthEnabled = !empty(webAuthClientId)
// /subscriptions/{sub}/resourceGroups/{rg}/providers/Microsoft.CognitiveServices/accounts/{name}
var foundryParts = split(foundryAccountId, '/')
var foundrySubscriptionId = empty(foundryAccountId) ? subscription().subscriptionId : foundryParts[2]
var foundryResourceGroup = empty(foundryAccountId) ? resourceGroup().name : foundryParts[4]
var foundryAccountName = empty(foundryAccountId) ? '' : last(foundryParts)

resource logs 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: 'log-${suffix}'
  location: location
  tags: tags
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 30
    // Stop ingesting for the day beyond 0.2 GB: the free allowance is 5 GB a month, and a logging loop must not turn into a bill.
    workspaceCapping: { dailyQuotaGb: json('0.2') }
  }
}

resource appInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: 'appi-${suffix}'
  location: location
  tags: tags
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: logs.id
  }
}

resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' = {
  name: 'cr${suffix}'
  location: location
  tags: tags
  sku: { name: 'Basic' }
  properties: { adminUserEnabled: false }
}

// One identity per workload, so each gets only the roles it needs
resource webIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: 'id-web-${suffix}'
  location: location
  tags: tags
}

resource workerIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: 'id-worker-${suffix}'
  location: location
  tags: tags
}

resource replayIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: 'id-replay-${suffix}'
  location: location
  tags: tags
}

resource acrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(registry.id, webIdentity.id, acrPullRoleId)
  scope: registry
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', acrPullRoleId)
    principalId: webIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

resource workerAcrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(registry.id, workerIdentity.id, acrPullRoleId)
  scope: registry
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', acrPullRoleId)
    principalId: workerIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

resource replayAcrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(registry.id, replayIdentity.id, acrPullRoleId)
  scope: registry
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', acrPullRoleId)
    principalId: replayIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

// Transport: one topic, sessions per match keep events in order (session id = match_id)
resource serviceBus 'Microsoft.ServiceBus/namespaces@2024-01-01' = {
  name: 'sb-${suffix}'
  location: location
  tags: tags
  sku: { name: 'Standard', tier: 'Standard' }
  properties: {
    disableLocalAuth: true
    minimumTlsVersion: '1.2'
  }
}

resource topic 'Microsoft.ServiceBus/namespaces/topics@2024-01-01' = {
  parent: serviceBus
  name: topicName
  properties: {
    defaultMessageTimeToLive: 'P1D'
  }
}

resource workerSubscription 'Microsoft.ServiceBus/namespaces/topics/subscriptions@2024-01-01' = {
  parent: topic
  name: workerSubscriptionName
  properties: {
    requiresSession: true
    maxDeliveryCount: 10
    deadLetteringOnMessageExpiration: true
    lockDuration: 'PT1M'
  }
}

// Worker reads its own subscription only; the KEDA scaler uses the same identity to read the message count
resource workerReceiver 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(workerSubscription.id, workerIdentity.id, serviceBusReceiverRoleId)
  scope: workerSubscription
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', serviceBusReceiverRoleId)
    principalId: workerIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

// Only the replay job publishes to the topic; the web app has no Service Bus role
resource replaySender 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(topic.id, replayIdentity.id, serviceBusSenderRoleId)
  scope: topic
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', serviceBusSenderRoleId)
    principalId: replayIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

// State: events, cards and audit trail as documents, partitioned by match
resource cosmos 'Microsoft.DocumentDB/databaseAccounts@2024-11-15' = {
  name: 'cosmos-${suffix}'
  location: location
  tags: tags
  kind: 'GlobalDocumentDB'
  properties: {
    databaseAccountOfferType: 'Standard'
    enableFreeTier: cosmosFreeTier
    disableLocalAuth: true
    minimalTlsVersion: 'Tls12'
    consistencyPolicy: { defaultConsistencyLevel: 'Session' }
    locations: [
      {
        locationName: location
        failoverPriority: 0
        isZoneRedundant: false
      }
    ]
  }
}

resource cosmosDatabase 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases@2024-11-15' = {
  parent: cosmos
  name: cosmosDatabaseName
  properties: {
    resource: { id: cosmosDatabaseName }
    // Shared by all containers; 1000 RU/s is what the free tier covers
    options: { throughput: 1000 }
  }
}

resource containers 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers@2024-11-15' = [for name in cosmosContainers: {
  parent: cosmosDatabase
  name: name
  properties: {
    resource: {
      id: name
      partitionKey: { paths: [ '/match_id' ], kind: 'Hash' }
      // The 360 frames are read with their event, never queried: indexing every position only costs request units on each write.
      indexingPolicy: {
        indexingMode: 'consistent'
        includedPaths: [ { path: '/*' } ]
        excludedPaths: [ { path: '/freeze_frame/*' }, { path: '/"_etag"/?' } ]
      }
    }
  }
}]

resource webCosmosAccess 'Microsoft.DocumentDB/databaseAccounts/sqlRoleAssignments@2024-11-15' = {
  parent: cosmos
  name: guid(cosmos.id, webIdentity.id, cosmosDataContributorRoleId)
  properties: {
    roleDefinitionId: '${cosmos.id}/sqlRoleDefinitions/${cosmosDataContributorRoleId}'
    principalId: webIdentity.properties.principalId
    scope: cosmos.id
  }
}

// Worker: data access limited to the watershed database
resource workerCosmosAccess 'Microsoft.DocumentDB/databaseAccounts/sqlRoleAssignments@2024-11-15' = {
  parent: cosmos
  name: guid(cosmos.id, workerIdentity.id, cosmosDataContributorRoleId)
  properties: {
    roleDefinitionId: '${cosmos.id}/sqlRoleDefinitions/${cosmosDataContributorRoleId}'
    principalId: workerIdentity.properties.principalId
    scope: '${cosmos.id}/dbs/${cosmosDatabaseName}'
  }
}

resource devCosmosAccess 'Microsoft.DocumentDB/databaseAccounts/sqlRoleAssignments@2024-11-15' = if (!empty(principalId)) {
  parent: cosmos
  name: guid(cosmos.id, principalId, cosmosDataContributorRoleId)
  properties: {
    roleDefinitionId: '${cosmos.id}/sqlRoleDefinitions/${cosmosDataContributorRoleId}'
    principalId: principalId
    scope: cosmos.id
  }
}

resource devServiceBusRoles 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for roleId in [ serviceBusSenderRoleId, serviceBusReceiverRoleId ]: if (!empty(principalId)) {
  name: guid(serviceBus.id, principalId, roleId)
  scope: serviceBus
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleId)
    principalId: principalId
    principalType: 'User'
  }
}]

// Foundry is in another resource group: the role assignment is deployed there
module workerFoundryAccess 'modules/foundry-role.bicep' = if (!empty(foundryAccountId)) {
  name: 'worker-foundry-role'
  scope: resourceGroup(foundrySubscriptionId, foundryResourceGroup)
  params: {
    accountName: foundryAccountName
    principalId: workerIdentity.properties.principalId
    roleDefinitionId: foundryRoleId
  }
}

resource env 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: 'cae-${suffix}'
  location: location
  tags: tags
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logs.properties.customerId
        sharedKey: logs.listKeys().primarySharedKey
      }
    }
  }
}

resource web 'Microsoft.App/containerApps@2025-01-01' = {
  name: 'ca-web-${suffix}'
  location: location
  tags: union(tags, { 'azd-service-name': 'web' })
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${webIdentity.id}': {} }
  }
  dependsOn: [ acrPull ]
  properties: {
    managedEnvironmentId: env.id
    configuration: {
      ingress: {
        external: true
        targetPort: 80
        transport: 'auto'
      }
      registries: [
        {
          server: registry.properties.loginServer
          identity: webIdentity.id
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'web'
          // Placeholder until `azd deploy` pushes the real image (port 80 matches it).
          image: webImage
          resources: {
            cpu: json('0.25')
            memory: '0.5Gi'
          }
          env: [
            {
              name: 'APPLICATIONINSIGHTS_CONNECTION_STRING'
              value: appInsights.properties.ConnectionString
            }
            {
              // Lets DefaultAzureCredential pick the user-assigned identity
              name: 'AZURE_CLIENT_ID'
              value: webIdentity.properties.clientId
            }
            {
              name: 'COSMOS_ENDPOINT'
              value: cosmos.properties.documentEndpoint
            }
            {
              name: 'COSMOS_DATABASE'
              value: cosmosDatabaseName
            }
            {
              name: 'DESK'
              value: 'cosmos'
            }
            {
              // Read-only console for visitors: producer actions need a signed-in producer (src/web/security.py)
              name: 'PUBLIC_MODE'
              value: '1'
            }
            {
              // Trust X-MS-CLIENT-PRINCIPAL-NAME only when Easy Auth is on: without it, a client can send the header itself.
              name: 'EASY_AUTH_ENABLED'
              value: string(easyAuthEnabled)
            }
            {
              name: 'PRODUCERS'
              value: webProducers
            }
            {
              name: 'RATE_LIMIT_PER_MIN'
              value: string(webRateLimitPerMin)
            }
          ]
        }
      ]
      scale: {
        minReplicas: webMinReplicas
        maxReplicas: 2
      }
    }
  }
}

// Easy Auth, off unless webAuthClientId is set. Anonymous requests pass through so the overlay stays public;
// signed-in requests carry X-MS-CLIENT-PRINCIPAL-NAME, which the app checks on producer routes.
// No client secret: the provider uses the ID token (OpenID Connect), enough to know who the producer is.
resource webAuth 'Microsoft.App/containerApps/authConfigs@2025-01-01' = if (easyAuthEnabled) {
  parent: web
  name: 'current'
  properties: {
    platform: { enabled: true }
    globalValidation: {
      unauthenticatedClientAction: 'AllowAnonymous'
    }
    identityProviders: {
      azureActiveDirectory: {
        enabled: true
        registration: {
          clientId: webAuthClientId
          openIdIssuer: '${environment().authentication.loginEndpoint}${tenant().tenantId}/v2.0'
        }
        validation: {
          allowedAudiences: [ 'api://${webAuthClientId}' ]
        }
      }
    }
  }
}

var workerEnv = [
  {
    name: 'AZURE_CLIENT_ID'
    value: workerIdentity.properties.clientId
  }
  {
    name: 'SERVICE_BUS_FQDN'
    value: serviceBusFqdn
  }
  {
    name: 'SERVICE_BUS_TOPIC'
    value: topicName
  }
  {
    name: 'COSMOS_ENDPOINT'
    value: cosmos.properties.documentEndpoint
  }
  {
    name: 'COSMOS_DATABASE'
    value: cosmosDatabaseName
  }
  {
    name: 'FOUNDRY_PROJECT_ENDPOINT'
    value: foundryProjectEndpoint
  }
  {
    name: 'FOUNDRY_MODEL'
    value: foundryModel
  }
  {
    name: 'FOUNDRY_MODEL_FALLBACK'
    value: foundryModelFallback
  }
  {
    name: 'FOUNDRY_REASONING_EFFORT'
    value: foundryReasoningEffort
  }
  {
    // Appended to the worker command by src/worker/Dockerfile
    name: 'WORKER_EXTRA_ARGS'
    value: workerExtraArgs
  }
]

// Worker: no ingress, scaled by KEDA on the worker subscription (0 when idle, 1 at most so one consumer owns each match)
resource worker 'Microsoft.App/containerApps@2025-01-01' = {
  name: 'ca-worker-${suffix}'
  location: location
  tags: union(tags, { 'azd-service-name': 'worker' })
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${workerIdentity.id}': {} }
  }
  dependsOn: [ workerAcrPull, workerReceiver ]
  properties: {
    managedEnvironmentId: env.id
    configuration: {
      activeRevisionsMode: 'Single'
      registries: [
        {
          server: registry.properties.loginServer
          identity: workerIdentity.id
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'worker'
          image: workerImage
          resources: {
            cpu: json('0.5')
            memory: '1Gi'
          }
          env: workerEnv
        }
      ]
      scale: {
        minReplicas: 0
        maxReplicas: 1
        // KEDA stops the replica 15 minutes after it last saw a message waiting (5 by default). The worker settles messages
        // within seconds, so a quiet stretch of a replay can look idle, and a replica stopped mid-match loses the match state.
        cooldownPeriod: 900
        rules: [
          {
            name: 'match-events'
            custom: {
              type: 'azure-servicebus'
              metadata: {
                namespace: serviceBus.name
                topicName: topicName
                subscriptionName: workerSubscriptionName
                messageCount: '1'
              }
              identity: workerIdentity.id
            }
          }
        ]
      }
    }
  }
}

// Replay: one manual run publishes the demo match to the topic, with the worker image
resource replayJob 'Microsoft.App/jobs@2025-01-01' = {
  name: 'caj-replay-${suffix}'
  location: location
  tags: tags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${replayIdentity.id}': {} }
  }
  dependsOn: [ replayAcrPull, replaySender ]
  properties: {
    environmentId: env.id
    configuration: {
      triggerType: 'Manual'
      manualTriggerConfig: {
        parallelism: 1
        replicaCompletionCount: 1
      }
      // Key moments play in real time: the whole match takes about half an hour at speed 10
      replicaTimeout: 3600
      replicaRetryLimit: 0
      registries: [
        {
          server: registry.properties.loginServer
          identity: replayIdentity.id
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'replay'
          image: workerImage
          command: [ 'python', '-m', 'generator.publish', 'data/replay/demo-0007.jsonl.gz', '--speed', '10' ]
          resources: {
            cpu: json('0.25')
            memory: '0.5Gi'
          }
          env: [
            {
              name: 'AZURE_CLIENT_ID'
              value: replayIdentity.properties.clientId
            }
            {
              name: 'SERVICE_BUS_FQDN'
              value: serviceBusFqdn
            }
            {
              name: 'SERVICE_BUS_TOPIC'
              value: topicName
            }
          ]
        }
      ]
    }
  }
}

output registryLoginServer string = registry.properties.loginServer
output registryName string = registry.name
output serviceBusFqdn string = serviceBusFqdn
output serviceBusTopic string = topicName
output cosmosEndpoint string = cosmos.properties.documentEndpoint
output cosmosDatabase string = cosmosDatabaseName
output webUri string = 'https://${web.properties.configuration.ingress.fqdn}'
output webAppName string = web.name
output workerAppName string = worker.name
output replayJobName string = replayJob.name
