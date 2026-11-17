// =====================================================================================
// Foundry resources (resource-group scope): AIServices account + project +
// gpt-5.4-mini deployment + developer data-plane RBAC. MI-only (no keys).
// =====================================================================================

@description('Azure region.')
param location string

@description('Base name for the Foundry (AIServices) account.')
param foundryAccountName string

@description('Foundry project name.')
param projectName string

@description('Developer principal object ID for data-plane RBAC.')
param developerPrincipalId string

@allowed([ 'User', 'Group', 'ServicePrincipal' ])
param developerPrincipalType string

@description('Model deployment capacity (TPM units, thousands).')
param modelCapacity int

param modelDeploymentName string = 'gpt-5.4-mini'
param assistModelDeploymentName string = 'gpt-4.1-mini'
param insightsModelDeploymentName string = 'gpt-5.4-nano'
param deployChatModel bool = true
param deployAssistModel bool = false
param deployInsightsModel bool = false

@description('Shared application managed identity; omitted by the standalone Foundry bootstrap.')
param runtimePrincipalId string = ''

@description('Resource tags.')
param tags object

// Globally-unique custom subdomain (required for Entra/token auth on Cognitive accounts).
var uniqueSuffix = toLower(uniqueString(resourceGroup().id, foundryAccountName))
var accountName = '${foundryAccountName}-${uniqueSuffix}'
var customSubDomain = accountName

// Built-in role definition IDs.
var roleCognitiveServicesOpenAiUser = '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd' // call Responses/OpenAI data plane
var roleAzureAiDeveloper = '64702f94-c441-49e6-a78b-ef80e0188fee' // Foundry/AI dev data plane
var roleFoundryUser = '53ca6127-db72-4b80-b1b0-d745d6d5456d'

// ---- Foundry (AIServices) account ----
resource account 'Microsoft.CognitiveServices/accounts@2025-06-01' = {
  name: accountName
  location: location
  tags: tags
  kind: 'AIServices'
  sku: {
    name: 'S0'
  }
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    customSubDomainName: customSubDomain
    allowProjectManagement: true // enable Foundry projects
    disableLocalAuth: true // Microsoft Entra ID and managed identities only; no keys
    publicNetworkAccess: 'Enabled' // public endpoint with keyless access; Cosmos uses a private endpoint
  }
}

// ---- Foundry project ----
resource project 'Microsoft.CognitiveServices/accounts/projects@2025-06-01' = {
  parent: account
  name: projectName
  location: location
  tags: tags
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    displayName: projectName
    description: 'Contact center Foundry project'
  }
}

// ---- gpt-5.4-mini deployment: Global Standard + priority processing ----
// reasoning effort = 'low' is set per-request in app code (Responses API), not here.
resource gpt54mini 'Microsoft.CognitiveServices/accounts/deployments@2026-03-15-preview' = if (deployChatModel) {
  parent: account
  name: modelDeploymentName
  sku: {
    name: 'GlobalStandard'
    capacity: modelCapacity
  }
  properties: {
    model: {
      format: 'OpenAI'
      name: 'gpt-5.4-mini'
      version: '2026-03-17'
    }
    serviceTier: 'Priority' // priority processing (deployment-level default)
    versionUpgradeOption: 'OnceCurrentVersionExpired'
  }
  dependsOn: [project]
}

// Concurrent model writes can conflict on the parent account.
resource assistModel 'Microsoft.CognitiveServices/accounts/deployments@2026-03-15-preview' = if (deployAssistModel) {
  parent: account
  name: assistModelDeploymentName
  sku: {
    name: 'GlobalStandard'
    capacity: 30
  }
  properties: {
    model: {
      format: 'OpenAI'
      name: 'gpt-4.1-mini'
      version: '2025-04-14'
    }
    versionUpgradeOption: 'OnceCurrentVersionExpired'
  }
  dependsOn: [project, gpt54mini]
}

resource insightsModel 'Microsoft.CognitiveServices/accounts/deployments@2026-03-15-preview' = if (deployInsightsModel) {
  parent: account
  name: insightsModelDeploymentName
  sku: {
    name: 'GlobalStandard'
    capacity: 50
  }
  properties: {
    model: {
      format: 'OpenAI'
      name: 'gpt-5.4-nano'
      version: '2026-03-17'
    }
    versionUpgradeOption: 'OnceCurrentVersionExpired'
  }
  dependsOn: [project, gpt54mini, assistModel]
}

resource runtimeRoles 'Microsoft.Authorization/roleAssignments@2022-04-01' = [
  for roleId in [roleCognitiveServicesOpenAiUser, roleAzureAiDeveloper]: if (!empty(runtimePrincipalId)) {
    scope: account
    name: guid(account.id, runtimePrincipalId, roleId)
    properties: {
      principalId: runtimePrincipalId
      principalType: 'ServicePrincipal'
      roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleId)
    }
  }
]

resource runtimeProjectAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(runtimePrincipalId)) {
  scope: project
  name: guid(project.id, runtimePrincipalId, roleFoundryUser)
  properties: {
    principalId: runtimePrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleFoundryUser)
  }
}

resource developerProjectAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: project
  name: guid(project.id, developerPrincipalId, roleFoundryUser)
  properties: {
    principalId: developerPrincipalId
    principalType: developerPrincipalType
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleFoundryUser)
  }
}

// ---- Developer data-plane RBAC (call the Responses API with Entra/MI) ----
resource raOpenAiUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: account
  name: guid(account.id, developerPrincipalId, roleCognitiveServicesOpenAiUser)
  properties: {
    principalId: developerPrincipalId
    principalType: developerPrincipalType
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleCognitiveServicesOpenAiUser)
  }
}

resource raAiDeveloper 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: account
  name: guid(account.id, developerPrincipalId, roleAzureAiDeveloper)
  properties: {
    principalId: developerPrincipalId
    principalType: developerPrincipalType
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleAzureAiDeveloper)
  }
}

output accountName string = account.name
output accountEndpoint string = account.properties.endpoint
output projectEndpoint string = 'https://${customSubDomain}.services.ai.azure.com/api/projects/${projectName}'
output openAiEndpoint string = 'https://${customSubDomain}.openai.azure.com/'
output cognitiveServicesEndpoint string = 'https://${customSubDomain}.cognitiveservices.azure.com/'
output modelDeploymentName string = deployChatModel ? gpt54mini!.name : ''
output accountPrincipalId string = account.identity.principalId
output projectPrincipalId string = project.identity.principalId
