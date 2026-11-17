// =====================================================================================
// Foundry (Microsoft Foundry v2) provisioning, subscription scope.
// Creates rg-moacc and a MINIMAL Foundry: AIServices account + project + gpt-5.4-mini.
//
// Grounded in MS Learn (June 2026):
//  - Foundry resource = Microsoft.CognitiveServices/accounts kind=AIServices (no new RP).
//  - Project = accounts/projects (child, own managed identity).
//  - gpt-5.4-mini priority processing = deployment property serviceTier:'Priority'
//    (SKU stays GlobalStandard); low reasoning effort is a per-request param.
//  - Key-based auth is disabled everywhere (disableLocalAuth=true); access uses Microsoft
//    Entra ID and managed identities. The Foundry endpoint stays public, MI-only; private
//    endpoints are out of scope for this module.
// =====================================================================================

targetScope = 'subscription'

@description('Azure region. Must support gpt-5.4-mini Global Standard + Responses API + priority processing.')
param location string = 'swedencentral'

@description('Resource group name for the shared contact center platform.')
param resourceGroupName string = 'rg-moacc'

@description('Base name for the Foundry (AIServices) account. A unique suffix is appended for global uniqueness.')
param foundryAccountName string = 'aif-moacc'

@description('Foundry project name.')
param projectName string = 'moacc'

@description('Object ID of the developer principal (you) to grant data-plane access for local development.')
param developerPrincipalId string

@description('Principal type of developerPrincipalId.')
@allowed([ 'User', 'Group', 'ServicePrincipal' ])
param developerPrincipalType string = 'User'

@description('gpt-5.4-mini deployment capacity in TPM units (thousands of tokens/min).')
param modelCapacity int = 50

var tags = {
  application: 'moacc'
  component: 'foundry'
  managedBy: 'bicep'
}

resource rg 'Microsoft.Resources/resourceGroups@2024-03-01' = {
  name: resourceGroupName
  location: location
  tags: tags
}

module foundry 'foundry.bicep' = {
  scope: rg
  name: 'moacc-foundry'
  params: {
    location: location
    foundryAccountName: foundryAccountName
    projectName: projectName
    developerPrincipalId: developerPrincipalId
    developerPrincipalType: developerPrincipalType
    modelCapacity: modelCapacity
    tags: tags
  }
}

output resourceGroupName string = rg.name
output foundryAccountName string = foundry.outputs.accountName
output foundryAccountEndpoint string = foundry.outputs.accountEndpoint
output foundryProjectEndpoint string = foundry.outputs.projectEndpoint
output modelDeploymentName string = foundry.outputs.modelDeploymentName
