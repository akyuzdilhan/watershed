param location string
param tags object

@description('Object id of the developer running azd, granted data access for local runs. Empty to skip.')
param principalId string = ''

@description('Cosmos DB free tier (1000 RU/s, 25 GB). Only one account per subscription can use it.')
param cosmosFreeTier bool = true

var suffix = uniqueString(resourceGroup().id)
var acrPullRoleId = '7f951dda-4ed3-4680-a7ca-43fe172d538d'
var serviceBusSenderRoleId = '69a216fc-b8fb-44d8-bc22-1f3c2cd27a39'
var serviceBusReceiverRoleId = '4f6d3b9b-027b-4f4c-9142-0e5a2a2247e0'
// Built-in Cosmos DB data-plane role: Cosmos DB Built-in Data Contributor
var cosmosDataContributorRoleId = '00000000-0000-0000-0000-000000000002'
var topicName = 'match-events'
var cosmosDatabaseName = 'watershed'
var cosmosContainers = [ 'events', 'cards', 'audit' ]

resource logs 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: 'log-${suffix}'
  location: location
  tags: tags
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 30
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

resource webIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: 'id-web-${suffix}'
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
  name: 'worker'
  properties: {
    requiresSession: true
    maxDeliveryCount: 10
    deadLetteringOnMessageExpiration: true
    lockDuration: 'PT1M'
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

resource web 'Microsoft.App/containerApps@2024-03-01' = {
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
          // Placeholder until `azd deploy` pushes the real image.
          image: 'mcr.microsoft.com/azuredocs/containerapps-helloworld:latest'
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
          ]
        }
      ]
      scale: {
        minReplicas: 0
        maxReplicas: 2
      }
    }
  }
}

output registryLoginServer string = registry.properties.loginServer
output registryName string = registry.name
output serviceBusFqdn string = '${serviceBus.name}.servicebus.windows.net'
output serviceBusTopic string = topicName
output cosmosEndpoint string = cosmos.properties.documentEndpoint
output cosmosDatabase string = cosmosDatabaseName
output webUri string = 'https://${web.properties.configuration.ingress.fqdn}'
