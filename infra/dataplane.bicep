metadata description = 'Unified data backbone: private-endpoint Cosmos (Entra-only) + VNet + private DNS + VNet-integrated ACA env + SignalR, for the agent-assist console and unified voice/text transcripts.'

@description('Primary location.')
param location string
param cosmosLocation string = location

@description('Unique resource name token (shared with the Phase 1 stack).')
param resourceToken string

@description('Tags applied to all resources.')
param tags object
param deployAgentAssist bool = false
param deployCrm bool = false
param publicEnvironmentResourceId string

@description('Resource id of the shared user-assigned managed identity (id-moacc).')
param uamiResourceId string

@description('Principal id of the shared UAMI — granted Cosmos data-plane access.')
param uamiPrincipalId string

@description('Log Analytics workspace customer id (GUID) for the VNet ACA env.')
param logAnalyticsCustomerId string

@secure()
@description('Log Analytics primary shared key for the VNet ACA env.')
param logAnalyticsSharedKey string

@description('Application Insights connection string for the VNet ACA env.')
param appInsightsConnectionString string

@description('ACR login server for assist image pulls.')
param registryServer string = ''

@description('UAMI client id used by the assist app to select its managed identity.')
param uamiClientId string = ''

param crmDataverseOrgUrl string = ''
param crmNativeWorkstreamIds string = ''

@secure()
@description('Shared ingestion API key (X-API-Key) validated by the assist console.')
param ingestApiKey string = ''

@description('Assist console container image (azd injects the built image).')
param assistImage string = 'mcr.microsoft.com/azuredocs/containerapps-helloworld:latest'

@description('Azure OpenAI endpoint for the assist console (managed-identity auth).')
param assistOpenAiEndpoint string

@description('Azure OpenAI deployment for assist non-insights calls (sentiment/classifiers/knowledge).')
param assistOpenAiDeployment string = 'gpt-4.1-mini'

@description('Azure OpenAI deployment for the realtime insights engine (lightweight reasoning model).')
param assistInsightsDeployment string = 'gpt-5.4-nano'

@description('Azure OpenAI API version (must support reasoning_effort for the insights model).')
param assistOpenAiApiVersion string = '2025-04-01-preview'
param knowledgeEndpoint string = ''
param knowledgeMode string = 'web'

// Cosmos DB built-in data-plane role: "Cosmos DB Built-in Data Contributor".
var cosmosDataContributorRoleId = '00000000-0000-0000-0000-000000000002'
var cosmosDatabaseName = 'agentassist'
var cosmosContainers = [
  'conversations'
  'messages'
  'insights'
]
var documentsPrivateDnsZoneName = 'privatelink.documents.azure.com'

// ---------------- VNet (ACA env subnet + private-endpoint subnet) ----------------
resource vnet 'Microsoft.Network/virtualNetworks@2024-05-01' = if (deployAgentAssist) {
  name: 'vnet-moacc-${resourceToken}'
  location: location
  tags: tags
  properties: {
    addressSpace: {
      addressPrefixes: ['10.20.0.0/16']
    }
    subnets: [
      {
        // ACA workload-profile environment infrastructure subnet (must be delegated).
        name: 'snet-aca'
        properties: {
          addressPrefix: '10.20.0.0/23'
          delegations: [
            {
              name: 'aca-delegation'
              properties: {
                serviceName: 'Microsoft.App/environments'
              }
            }
          ]
        }
      }
      {
        // Private endpoints subnet.
        name: 'snet-pe'
        properties: {
          addressPrefix: '10.20.2.0/24'
          privateEndpointNetworkPolicies: 'Disabled'
        }
      }
    ]
  }
}

resource acaSubnet 'Microsoft.Network/virtualNetworks/subnets@2024-05-01' existing = {
  parent: vnet
  name: 'snet-aca'
}

resource peSubnet 'Microsoft.Network/virtualNetworks/subnets@2024-05-01' existing = {
  parent: vnet
  name: 'snet-pe'
}

// ---------------- Cosmos DB (serverless, Entra-only, private) ----------------
resource cosmos 'Microsoft.DocumentDB/databaseAccounts@2024-11-15' = if (deployAgentAssist) {
  name: 'cosno-moacc-${resourceToken}-${cosmosLocation}'
  location: cosmosLocation
  tags: tags
  kind: 'GlobalDocumentDB'
  properties: {
    databaseAccountOfferType: 'Standard'
    enableAutomaticFailover: true
    minimalTlsVersion: 'Tls12'
    consistencyPolicy: {
      defaultConsistencyLevel: 'Session'
    }
    locations: [
      {
        locationName: cosmosLocation
        failoverPriority: 0
        isZoneRedundant: false
      }
    ]
    capabilities: [
      {
        name: 'EnableServerless'
      }
    ]
    // Cosmos accepts Microsoft Entra ID only, with no public network access.
    disableLocalAuth: true
    publicNetworkAccess: 'Disabled'
    networkAclBypass: 'AzureServices'
    isVirtualNetworkFilterEnabled: false
  }
}

resource cosmosDb 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases@2024-11-15' = if (deployAgentAssist) {
  parent: cosmos
  name: cosmosDatabaseName
  properties: {
    resource: {
      id: cosmosDatabaseName
    }
  }
}

resource containers 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers@2024-11-15' = [
  for c in cosmosContainers: if (deployAgentAssist) {
    parent: cosmosDb
    name: c
    properties: {
      resource: {
        id: c
        partitionKey: {
          paths: ['/conversationId']
          kind: 'Hash'
        }
        indexingPolicy: {
          indexingMode: 'consistent'
          automatic: true
          includedPaths: [{ path: '/*' }]
        }
      }
    }
  }
]

resource cosmosDataRole 'Microsoft.DocumentDB/databaseAccounts/sqlRoleAssignments@2024-11-15' = if (deployAgentAssist) {
  parent: cosmos
  name: guid(cosmos.id, uamiPrincipalId, cosmosDataContributorRoleId)
  properties: {
    roleDefinitionId: resourceId('Microsoft.DocumentDB/databaseAccounts/sqlRoleDefinitions', cosmos.name, cosmosDataContributorRoleId)
    principalId: uamiPrincipalId
    scope: cosmos.id
  }
}

// ---------------- Private endpoint + private DNS for Cosmos ----------------
resource cosmosPe 'Microsoft.Network/privateEndpoints@2024-05-01' = if (deployAgentAssist) {
  name: 'pep-cosmos-moacc-${resourceToken}'
  location: location
  tags: tags
  properties: {
    subnet: {
      id: peSubnet.id
    }
    privateLinkServiceConnections: [
      {
        name: 'cosmos-sql'
        properties: {
          privateLinkServiceId: cosmos.id
          groupIds: ['Sql']
        }
      }
    ]
  }
}

resource documentsDnsZone 'Microsoft.Network/privateDnsZones@2020-06-01' = if (deployAgentAssist) {
  name: documentsPrivateDnsZoneName
  location: 'global'
  tags: tags
}

resource documentsDnsLink 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2020-06-01' = if (deployAgentAssist) {
  parent: documentsDnsZone
  name: 'link-${resourceToken}'
  location: 'global'
  tags: tags
  properties: {
    registrationEnabled: false
    virtualNetwork: {
      id: vnet.id
    }
  }
}

resource cosmosPeDns 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2024-05-01' = if (deployAgentAssist) {
  parent: cosmosPe
  name: 'default'
  properties: {
    privateDnsZoneConfigs: [
      {
        name: 'documents'
        properties: {
          privateDnsZoneId: documentsDnsZone.id
        }
      }
    ]
  }
}

// ---------------- VNet-integrated ACA environment (for the assist console) ----------------
resource acaEnvVnet 'Microsoft.App/managedEnvironments@2024-10-02-preview' = if (deployAgentAssist) {
  name: 'cae-moacc-vnet-${resourceToken}'
  location: location
  tags: tags
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logAnalyticsCustomerId
        sharedKey: logAnalyticsSharedKey
      }
    }
    daprAIConnectionString: appInsightsConnectionString
    vnetConfiguration: {
      infrastructureSubnetId: acaSubnet.id
      internal: false
    }
    workloadProfiles: [
      {
        name: 'Consumption'
        workloadProfileType: 'Consumption'
      }
    ]
    zoneRedundant: false
  }
}

// ---------------- SignalR (realtime push for the assist console) ----------------
resource signalr 'Microsoft.SignalRService/signalR@2024-03-01' = if (deployAgentAssist) {
  name: 'sigr-moacc-${resourceToken}'
  location: location
  tags: tags
  sku: {
    name: 'Standard_S1'
    tier: 'Standard'
    capacity: 1
  }
  kind: 'SignalR'
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${uamiResourceId}': {}
    }
  }
  properties: {
    features: [
      {
        flag: 'ServiceMode'
        value: 'Serverless'
      }
    ]
    cors: {
      allowedOrigins: ['*']
    }
  }
}

// ---------------- Agent-assist console (Next.js) on the VNet env ----------------
resource crmIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = if (deployCrm) {
  name: 'id-moacc-crm-${resourceToken}'
  location: location
  tags: tags
}

resource assistApp 'Microsoft.App/containerApps@2024-10-02-preview' = {
  name: 'ca-assist'
  location: location
  tags: union(tags, { 'azd-service-name': 'assist' })
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: union(
      { '${uamiResourceId}': {} },
      deployCrm ? { '${crmIdentity!.id}': {} } : {}
    )
  }
  properties: {
    managedEnvironmentId: deployAgentAssist ? acaEnvVnet!.id : publicEnvironmentResourceId
    workloadProfileName: 'Consumption'
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: true
        targetPort: 3000
        transport: 'auto'
        allowInsecure: false
      }
      registries: [
        {
          server: registryServer
          identity: uamiResourceId
        }
      ]
      secrets: concat(
        deployAgentAssist ? [{
          name: 'signalr-connection'
          value: signalr!.listKeys().primaryConnectionString
        }] : [],
        [{
          name: 'ingest-api-key'
          value: ingestApiKey
        }]
      )
    }
    template: {
      containers: [
        {
          name: 'assist'
          image: assistImage
          resources: {
            cpu: json('0.5')
            memory: '1Gi'
          }
          env: [
            { name: 'COSMOS_DB_ENDPOINT', value: deployAgentAssist ? cosmos!.properties.documentEndpoint : '' }
            { name: 'COSMOS_DB_DATABASE', value: deployAgentAssist ? cosmosDatabaseName : '' }
            { name: 'AZURE_CLIENT_ID', value: uamiClientId }
            { name: 'CRM_CLIENT_ID', value: deployCrm ? crmIdentity!.properties.clientId : '' }
            { name: 'CRM_DATAVERSE_ORG_URL', value: crmDataverseOrgUrl }
            { name: 'CRM_NATIVE_WORKSTREAM_IDS', value: crmNativeWorkstreamIds }
            { name: 'SIGNALR_CONNECTION_STRING', ...(deployAgentAssist ? { secretRef: 'signalr-connection' } : { value: '' }) }
            { name: 'API_KEY', secretRef: 'ingest-api-key' }
            { name: 'AZURE_OPENAI_ENDPOINT', value: assistOpenAiEndpoint }
            { name: 'AZURE_OPENAI_DEPLOYMENT', value: assistOpenAiDeployment }
            { name: 'AZURE_OPENAI_INSIGHTS_DEPLOYMENT', value: assistInsightsDeployment }
            { name: 'AZURE_OPENAI_API_VERSION', value: assistOpenAiApiVersion }
            { name: 'FOUNDRY_IQ_SEARCH_ENDPOINT', value: knowledgeEndpoint }
            { name: 'FOUNDRY_IQ_KNOWLEDGE_BASE', value: 'organization-knowledge' }
            { name: 'FOUNDRY_IQ_INDEXED_KNOWLEDGE_BASE', value: 'organization-indexed' }
            { name: 'FOUNDRY_IQ_KNOWLEDGE_SOURCE', value: 'approved-documents' }
            { name: 'FOUNDRY_IQ_WEB_SOURCE', value: 'approved-web' }
            { name: 'FOUNDRY_IQ_MODE', value: knowledgeMode }
            { name: 'NODE_ENV', value: 'production' }
          ]
        }
      ]
      scale: {
        minReplicas: 1
        maxReplicas: 3
      }
    }
  }
}

output cosmosEndpoint string = deployAgentAssist ? cosmos!.properties.documentEndpoint : ''
output cosmosDatabaseName string = deployAgentAssist ? cosmosDatabaseName : ''
output cosmosAccountName string = deployAgentAssist ? cosmos!.name : ''
output acaEnvVnetResourceId string = deployAgentAssist ? acaEnvVnet!.id : ''
output acaEnvVnetName string = deployAgentAssist ? acaEnvVnet!.name : ''
output signalRName string = deployAgentAssist ? signalr!.name : ''
output signalRHostName string = deployAgentAssist ? signalr!.properties.hostName : ''
output vnetName string = deployAgentAssist ? vnet!.name : ''
output assistFqdn string = assistApp.properties.configuration.ingress.fqdn
output crmUamiClientId string = deployCrm ? crmIdentity!.properties.clientId : ''
output crmUamiPrincipalId string = deployCrm ? crmIdentity!.properties.principalId : ''
output crmUamiResourceId string = deployCrm ? crmIdentity!.id : ''
