metadata description = 'Keyless Foundry IQ with approved organization sources and managed-identity model access.'

param location string
param resourceToken string
param tags object
param runtimePrincipalId string
param developerPrincipalId string
param projectPrincipalId string
param foundryAccountName string

resource search 'Microsoft.Search/searchServices@2025-05-01' = {
  name: 'srch-moacc-${resourceToken}'
  location: location
  tags: tags
  sku: {
    name: 'basic'
  }
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    replicaCount: 1
    partitionCount: 1
    hostingMode: 'Default'
    publicNetworkAccess: 'enabled'
    disableLocalAuth: true
    semanticSearch: 'standard'
  }
}

var readerRole = '1407120a-92aa-4202-b7e9-c0e197c71c8f'
var indexWriterRole = '8ebe5a00-799e-43f5-93ac-243d3dce84a7'
var serviceContributorRole = '7ca78c08-252a-4471-8644-bb5ff32d4ba0'

resource readers 'Microsoft.Authorization/roleAssignments@2022-04-01' = [
  for principalId in [runtimePrincipalId, projectPrincipalId]: {
    scope: search
    name: guid(search.id, principalId, readerRole)
    properties: {
      principalId: principalId
      principalType: 'ServicePrincipal'
      roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', readerRole)
    }
  }
]

resource developerRoles 'Microsoft.Authorization/roleAssignments@2022-04-01' = [
  for roleId in [indexWriterRole, serviceContributorRole]: {
    scope: search
    name: guid(search.id, developerPrincipalId, roleId)
    properties: {
      principalId: developerPrincipalId
      principalType: 'User'
      roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleId)
    }
  }
]

output endpoint string = 'https://${search.name}.search.windows.net'
output name string = search.name
output resourceId string = search.id

resource modelAccount 'Microsoft.CognitiveServices/accounts@2025-06-01' existing = {
  name: foundryAccountName
}

resource searchModelAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: modelAccount
  name: guid(modelAccount.id, search.id, '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd')
  properties: {
    principalId: search.identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd')
  }
}
